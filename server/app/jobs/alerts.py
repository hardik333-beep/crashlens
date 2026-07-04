"""Alert dispatch: the arq job that notifies a team when an Issue is new or regresses.

``app/jobs/process_event.py`` enqueues a ``dispatch_alerts`` job AFTER its own
transaction commits, and ONLY when the event created a new Issue or flipped a
resolved one to regressed (never on a duplicate or poison path). This module
consumes that job: it loads the org's enabled alert channels under RLS and fans
the notification out to each one, isolated so one failing channel never stops the
others.

CHANNEL SELECTION (org-wide vs project-scoped)
----------------------------------------------
A channel with ``project_id IS NULL`` is org-wide and fires for every project; a
channel with a ``project_id`` fires only for that project. Both are loaded in one
RLS-scoped query (``project_id IS NULL OR project_id = :project_id``).

DELIVERY BY TYPE
----------------
* ``email``: SMTP via ``smtplib`` run in a worker thread (``asyncio.to_thread``)
  so the blocking socket work never stalls the event loop. Email is OPTIONAL:
  if SMTP is not configured (no ``SMTP_HOST`` / ``SMTP_FROM``) the module logs a
  single WARNING once per process and skips every email channel. Recipients are
  the channel's explicit ``to`` list, or -- when it has none -- every current org
  member's email, resolved fresh at send time.
* ``slack``: an HTTP POST of ``{"text": message}`` to the channel's incoming
  webhook. ``httpx`` is a DEV-only dependency here (see pyproject), so this uses
  the standard-library ``urllib`` in a thread rather than promoting httpx to a
  runtime dependency.
* ``webhook``: an HTTP POST of a JSON payload with an ``X-Crashlens-Event``
  header, likewise via ``urllib`` in a thread.

SECRETS / PII HYGIENE
---------------------
A Slack or generic webhook URL can embed a token, so it is a SECRET and is NEVER
logged. A delivery failure logs ONLY the channel id and type -- never the URL,
never the recipient list, never the alert body or event payload.
"""

import asyncio
import datetime
import json
import logging
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import accounts
from app.config import Settings, get_settings
from app.db import tenant_session

logger = logging.getLogger(__name__)

# FLAGGED DEFAULTS (governor review): HTTP delivery timeout and retry budget for
# Slack / generic webhook posts. 5s is generous for an incoming-webhook POST
# without pinning a worker; one retry (2 attempts total) rides out a single
# transient blip without amplifying load on a genuinely-down endpoint.
_HTTP_TIMEOUT_SECONDS = 5
_HTTP_ATTEMPTS = 2

# Emitted once per process (not per event) when an email channel is encountered
# but SMTP is not configured, so a misconfiguration is visible without flooding
# the log on every alert. Reset only on process restart.
_smtp_warning_emitted = False


# ==============================================================================
# Pure formatting helpers (no I/O; unit-tested without any service).
# ==============================================================================


def alert_subject(kind: str, project_name: str, title: str) -> str:
    """Return the email subject line for a ``new`` or ``regression`` alert."""
    if kind == "regression":
        return f"[Crashlens] Error came back in {project_name}: {title}"
    return f"[Crashlens] New error in {project_name}: {title}"


def alert_link(
    public_base_url: str | None,
    org_id: str,
    project_id: str,
    issue_id: str,
) -> str:
    """Return the issue link: a relative path, prefixed by ``public_base_url`` if set.

    The relative path is always ``/org/{org}/projects/{project}/issues/{issue}``
    (the dashboard route). When a public base URL is configured it is prefixed
    (trailing slash trimmed) to make an absolute, clickable link.
    """
    path = f"/org/{org_id}/projects/{project_id}/issues/{issue_id}"
    if public_base_url:
        return f"{public_base_url.rstrip('/')}{path}"
    return path


def alert_body(
    kind: str,
    project_name: str,
    title: str,
    level: str,
    link: str,
    release: str | None = None,
) -> str:
    """Return the plain-text body shared by email and Slack messages.

    For a regression, when the came-back-in ``release`` is known the headline
    names it ("...came back in <release>"); otherwise it reads the same as before.
    """
    if kind == "regression":
        headline = "An error that was resolved has started happening again."
        if release:
            headline += f" It came back in {release}."
    else:
        headline = "A new error just showed up."
    body = (
        f"{headline}\n\n"
        f"Project: {project_name}\n"
        f"Error: {title}\n"
        f"Level: {level}\n"
    )
    if kind == "regression" and release:
        body += f"Release: {release}\n"
    return body + f"\nSee it here: {link}\n"


def webhook_payload(
    kind: str,
    project_id: str,
    issue_id: str,
    title: str,
    level: str,
    ts: str,
    release: str | None = None,
) -> dict:
    """Return the JSON body POSTed to a generic webhook channel.

    ``release`` (the came-back-in release for a regression) is included only when
    known, so a "new"-issue payload keeps its existing shape unchanged.
    """
    payload = {
        "kind": kind,
        "project_id": project_id,
        "issue_id": issue_id,
        "title": title,
        "level": level,
        "ts": ts,
    }
    if release is not None:
        payload["release"] = release
    return payload


def smtp_is_configured(settings: Settings) -> bool:
    """Return True only when email alerts can actually be sent.

    Both a host to connect to and a from-address are required; username/password
    are optional (an unauthenticated relay is valid).
    """
    return bool(settings.smtp_host) and bool(settings.smtp_from)


# ==============================================================================
# Blocking senders (run inside asyncio.to_thread). Monkeypatched wholesale in
# unit tests, so dispatch_alerts calls them by their MODULE name.
# ==============================================================================


def _post_json(url: str, data: dict, headers: dict[str, str]) -> None:
    """POST ``data`` as JSON to ``url`` with one retry. Blocking; call in a thread.

    Raises the last error if every attempt fails; the caller isolates that per
    channel. The URL is never logged here (it can embed a secret token).
    """
    body = json.dumps(data).encode("utf-8")
    all_headers = {"Content-Type": "application/json", **headers}
    last_error: Exception | None = None
    for _ in range(_HTTP_ATTEMPTS):
        request = urllib.request.Request(  # noqa: S310 - url is an operator-configured https webhook, validated https at store time
            url, data=body, headers=all_headers, method="POST"
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - see above
                request, timeout=_HTTP_TIMEOUT_SECONDS
            ) as response:
                # Drain and discard: a 2xx is success; the body is not used.
                response.read()
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
    if last_error is not None:
        raise last_error


def send_slack(webhook_url: str, text_message: str) -> None:
    """POST ``{"text": ...}`` to a Slack incoming webhook. Blocking; call in a thread."""
    _post_json(webhook_url, {"text": text_message}, headers={})


def send_webhook(url: str, payload: dict) -> None:
    """POST ``payload`` to a generic webhook with the event header. Blocking; in a thread."""
    _post_json(url, payload, headers={"X-Crashlens-Event": "issue_alert"})


def send_email(
    recipients: list[str], subject: str, body: str, settings: Settings
) -> None:
    """Send a plain-text email via SMTP. Blocking; call in a thread.

    Assumes SMTP is configured (the caller checks :func:`smtp_is_configured`
    first) and ``recipients`` is non-empty. STARTTLS and auth are applied when
    configured. Never logs the message or the recipient list.
    """
    message = EmailMessage()
    message["From"] = settings.smtp_from  # type: ignore[assignment]
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(
        settings.smtp_host, settings.smtp_port, timeout=_HTTP_TIMEOUT_SECONDS
    ) as server:
        if settings.smtp_starttls:
            server.starttls()
        if settings.smtp_username and settings.smtp_password:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(message)


# ==============================================================================
# Data loads (RLS-scoped) + the dispatch job.
# ==============================================================================


async def _load_channels_and_project(
    session: AsyncSession, project_id: str
) -> tuple[list[dict], str]:
    """Return (enabled matching channels, project name) inside the open tenant session.

    Channels match when they are org-wide (``project_id IS NULL``) or scoped to
    this project. The project name is used in the message; if the project row is
    gone (a race with deletion) the project id string stands in.
    """
    rows = (
        await session.execute(
            text(
                "SELECT id, type, config FROM alert_channels "
                "WHERE enabled = true "
                "AND (project_id IS NULL OR project_id = :pid)"
            ),
            {"pid": project_id},
        )
    ).all()
    channels = [{"id": row.id, "type": row.type, "config": row.config} for row in rows]

    name_row = (
        await session.execute(
            text("SELECT name FROM projects WHERE id = :pid"),
            {"pid": project_id},
        )
    ).one_or_none()
    project_name = name_row.name if name_row is not None else project_id
    return channels, project_name


async def _load_member_emails(
    org_id: str,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> list[str]:
    """Return every current org member's email (org-scoped read, plain-session emails).

    Memberships are read inside ``tenant_session`` (RLS-scoped); emails live on
    the RLS-exempt ``users`` table and are attached via a plain-session lookup,
    the same discipline as ``projects.list_members``.
    """
    async with tenant_session(org_id, session_factory=session_factory) as session:
        rows = (
            await session.execute(text("SELECT user_id FROM org_memberships"))
        ).all()
    user_ids = [row.user_id for row in rows]
    emails = await accounts.load_users_by_ids(user_ids, session_factory=session_factory)
    return [email for email in emails.values() if email]


async def dispatch_alerts(
    ctx: dict,
    *,
    org_id: str,
    project_id: str,
    issue_id: str,
    kind: str,
    title: str,
    level: str,
    release: str | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """Deliver a new-issue / regression alert to every enabled channel for the org.

    Producer contract (``app/jobs/process_event.py``): all ids are server-derived
    (never client input); ``kind`` is ``"new"`` or ``"regression"``; ``title`` and
    ``level`` describe the Issue; ``release`` is the came-back-in release on a
    regression (``None`` otherwise / for an untagged build). ``session_factory``
    is injectable for tests; arq calls this with only ``ctx`` + the keyword
    contract.

    Per-channel isolation: each delivery is wrapped so one failing channel logs a
    WARNING (channel id + type only, never the URL/recipients/body) and the rest
    still fire. Returns a small counters dict for logging/testing.
    """
    global _smtp_warning_emitted
    settings = get_settings()

    async with tenant_session(org_id, session_factory=session_factory) as session:
        channels, project_name = await _load_channels_and_project(session, project_id)

    if not channels:
        logger.info(
            "dispatch_alerts: no channels org_id=%s project_id=%s kind=%s",
            org_id,
            project_id,
            kind,
        )
        return {"delivered": 0, "failed": 0, "skipped": 0}

    subject = alert_subject(kind, project_name, title)
    link = alert_link(settings.public_base_url, org_id, project_id, issue_id)
    body = alert_body(kind, project_name, title, level, link, release)
    payload = webhook_payload(
        kind,
        project_id,
        issue_id,
        title,
        level,
        datetime.datetime.now(datetime.UTC).isoformat(),
        release,
    )

    # Resolve the default email recipient roster once, lazily, only if an email
    # channel without an explicit "to" is present.
    member_emails: list[str] | None = None
    email_configured = smtp_is_configured(settings)
    if email_configured and any(
        c["type"] == "email" and not (c["config"] or {}).get("to") for c in channels
    ):
        member_emails = await _load_member_emails(org_id, session_factory)

    delivered = failed = skipped = 0
    for channel in channels:
        channel_id = channel["id"]
        channel_type = channel["type"]
        config = channel["config"] or {}
        try:
            if channel_type == "email":
                if not email_configured:
                    if not _smtp_warning_emitted:
                        logger.warning(
                            "dispatch_alerts: SMTP is not configured; skipping email "
                            "alerts (set SMTP_HOST and SMTP_FROM to enable)"
                        )
                        _smtp_warning_emitted = True
                    skipped += 1
                    continue
                recipients = config.get("to") or member_emails or []
                if not recipients:
                    skipped += 1
                    continue
                await asyncio.to_thread(send_email, recipients, subject, body, settings)
            elif channel_type == "slack":
                await asyncio.to_thread(send_slack, config["webhook_url"], body)
            elif channel_type == "webhook":
                await asyncio.to_thread(send_webhook, config["url"], payload)
            else:
                skipped += 1
                continue
            delivered += 1
        except Exception:  # noqa: BLE001 - per-channel isolation boundary
            failed += 1
            logger.warning(
                "dispatch_alerts: channel delivery failed channel_id=%s type=%s",
                channel_id,
                channel_type,
            )

    logger.info(
        "dispatch_alerts: done org_id=%s project_id=%s kind=%s "
        "delivered=%d failed=%d skipped=%d",
        org_id,
        project_id,
        kind,
        delivered,
        failed,
        skipped,
    )
    return {"delivered": delivered, "failed": failed, "skipped": skipped}
