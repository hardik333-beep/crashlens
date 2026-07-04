"""Alert-channel service: the data-layer half of the alerts slice.

Route handlers in ``app/routes/alerts.py`` stay thin and call into these
functions. Like ``app/projects.py`` every function accepts an optional
``session_factory`` so integration tests can bind a NON-superuser engine and
exercise the real Row Level Security policies (production callers omit it).

SESSION CHOICE PER OPERATION (grounded in app/db.py's docstring):

* All alert-channel work is org-scoped and goes through ``tenant_session(org_id)``.
  The ``org_id`` is always the VERIFIED id from an ``OrgContext``
  (require_org_member / require_org_admin proved the caller's membership), never
  client input. RLS then filters every statement by that org, so no handler
  writes ``WHERE org_id = ...`` by hand.
* A ``project_id`` scope (the channel fires only for that project) is confirmed
  to belong to this org under RLS BEFORE the channel is written, so a channel can
  never be pinned to a project in another org.

SECRETS HYGIENE: a Slack incoming-webhook URL or a generic webhook URL can embed
a token in its path/query, so it is a SECRET. It is stored in the ``config``
jsonb column but NEVER returned in full by the read API: :func:`mask_target`
reduces it to scheme + host + "/..." for display. The UI edits a channel by
REPLACING the URL, never by reading it back.
"""

import datetime
import json
import re
import uuid
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import tenant_session

# The three channel types the schema's CHECK constraint allows
# (migration 0001: ck_alert_channels_type).
CHANNEL_TYPES = ("email", "slack", "webhook")

# Deliberately permissive address shape: a local-part, an "@", and a domain with
# a dot. Full RFC 5322 validation is not the goal (the SMTP server is the real
# authority); this only rejects obvious garbage before it is stored.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ChannelConfigError(ValueError):
    """A channel's config failed validation (maps to a 400 in the route)."""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _is_https_url(value: object) -> bool:
    """Return True if ``value`` is a syntactically valid https:// URL with a host.

    https is required (not plain http) because these URLs carry alert content and
    can embed a secret token; sending them in the clear would leak both.
    """
    if not isinstance(value, str) or not value:
        return False
    parts = urlsplit(value.strip())
    return parts.scheme == "https" and bool(parts.netloc)


def validate_channel_config(channel_type: str, config: object) -> dict:
    """Return a normalized config dict for ``channel_type`` or raise ``ChannelConfigError``.

    Rules (plain-language messages, safe to surface to the caller):

    * ``email``: config is optional. An optional ``to`` may be a list of email
      addresses; when omitted or empty the channel emails all current org members
      at send time (resolved fresh each alert, so it always tracks the roster).
    * ``slack``: requires ``webhook_url``, an https URL (the Slack incoming
      webhook).
    * ``webhook``: requires ``url``, an https URL.

    Only the recognized keys are kept, so a caller cannot smuggle extra fields
    into the stored jsonb.
    """
    if channel_type not in CHANNEL_TYPES:
        raise ChannelConfigError(
            f"Channel type must be one of {', '.join(CHANNEL_TYPES)}."
        )
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ChannelConfigError("Channel configuration must be an object.")

    if channel_type == "email":
        recipients = config.get("to")
        if recipients is None:
            return {}
        if not isinstance(recipients, list) or not all(
            isinstance(item, str) for item in recipients
        ):
            raise ChannelConfigError("The 'to' field must be a list of email addresses.")
        cleaned = [item.strip() for item in recipients if item.strip()]
        for address in cleaned:
            if not _EMAIL_RE.match(address):
                raise ChannelConfigError(f"'{address}' is not a valid email address.")
        return {"to": cleaned} if cleaned else {}

    if channel_type == "slack":
        url = config.get("webhook_url")
        if not _is_https_url(url):
            raise ChannelConfigError(
                "A Slack channel needs a 'webhook_url' that is an https URL."
            )
        return {"webhook_url": url.strip()}  # type: ignore[union-attr]

    # webhook
    url = config.get("url")
    if not _is_https_url(url):
        raise ChannelConfigError("A webhook needs a 'url' that is an https URL.")
    return {"url": url.strip()}  # type: ignore[union-attr]


def mask_target(channel_type: str, config: dict) -> str:
    """Return a display-safe summary of a channel's destination, never the secret URL.

    For email: the explicit recipient list, or a plain-language "All team
    members" when it defaults to the whole org. For slack/webhook: ``scheme +
    host + "/..."`` so the host is visible for recognition but the secret-bearing
    path and query are withheld (they can embed a token).
    """
    if channel_type == "email":
        recipients = config.get("to") if isinstance(config, dict) else None
        if isinstance(recipients, list) and recipients:
            return ", ".join(recipients)
        return "All team members"

    raw = ""
    if isinstance(config, dict):
        raw = config.get("webhook_url") or config.get("url") or ""
    if not isinstance(raw, str) or not raw:
        return "(not set)"
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return "(hidden)"
    return f"{parts.scheme}://{parts.netloc}/..."


def _channel_dict(row: object) -> dict:
    return {
        "id": row.id,  # type: ignore[attr-defined]
        "type": row.type,  # type: ignore[attr-defined]
        "project_id": row.project_id,  # type: ignore[attr-defined]
        "config": row.config,  # type: ignore[attr-defined]
        "enabled": row.enabled,  # type: ignore[attr-defined]
        "created_at": row.created_at,  # type: ignore[attr-defined]
    }


async def list_channels(
    org_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> list[dict]:
    """Return the org's alert channels, newest first, scoped by RLS to ``org_id``."""
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, type, project_id, config, enabled, created_at "
                    "FROM alert_channels ORDER BY created_at DESC"
                )
            )
        ).all()
    return [_channel_dict(row) for row in rows]


async def _load_project_id(
    session: AsyncSession, project_id: uuid.UUID
) -> uuid.UUID | None:
    """Return the project id if it is visible in this org under RLS, else None."""
    row = (
        await session.execute(
            text("SELECT id FROM projects WHERE id = :pid"),
            {"pid": str(project_id)},
        )
    ).one_or_none()
    return row.id if row is not None else None


async def create_channel(
    org_id: uuid.UUID,
    channel_type: str,
    config: object,
    project_id: uuid.UUID | None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict | None:
    """Create an alert channel and return it, or None if ``project_id`` is not in the org.

    Validates and normalizes ``config`` for the type (raises ``ChannelConfigError``
    on bad input, which the route maps to a 400). A ``project_id`` scope is
    confirmed to belong to this org under RLS before the INSERT, so a channel can
    never be pinned to another org's project (returns None -> 404 otherwise). The
    INSERT runs inside ``tenant_session(org_id)`` so WITH CHECK passes.
    """
    normalized = validate_channel_config(channel_type, config)
    channel_id = uuid.uuid4()

    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        if project_id is not None:
            if await _load_project_id(session, project_id) is None:
                return None
        row = (
            await session.execute(
                text(
                    "INSERT INTO alert_channels "
                    "(id, org_id, project_id, type, config, enabled) "
                    "VALUES (:id, :oid, :pid, :type, CAST(:config AS jsonb), true) "
                    "RETURNING id, type, project_id, config, enabled, created_at"
                ),
                {
                    "id": str(channel_id),
                    "oid": str(org_id),
                    "pid": str(project_id) if project_id is not None else None,
                    "type": channel_type,
                    "config": json.dumps(normalized),
                },
            )
        ).one()
    return _channel_dict(row)


async def update_channel(
    org_id: uuid.UUID,
    channel_id: uuid.UUID,
    *,
    enabled: bool | None = None,
    config: object = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict | None:
    """Enable/disable a channel and/or replace its config. Return the row, or None.

    The channel's own ``type`` (never changeable here) drives config validation,
    so the caller cannot repurpose a channel to another type. RLS scopes the read
    and the UPDATE to ``org_id``; a channel in another org is simply not found
    (None -> 404), never cross-tenant written.
    """
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        current = (
            await session.execute(
                text(
                    "SELECT id, type, project_id, config, enabled, created_at "
                    "FROM alert_channels WHERE id = :cid"
                ),
                {"cid": str(channel_id)},
            )
        ).one_or_none()
        if current is None:
            return None

        params: dict[str, object] = {"cid": str(channel_id)}
        assignments: list[str] = []
        if enabled is not None:
            assignments.append("enabled = :enabled")
            params["enabled"] = enabled
        if config is not None:
            normalized = validate_channel_config(current.type, config)
            assignments.append("config = CAST(:config AS jsonb)")
            params["config"] = json.dumps(normalized)

        if not assignments:
            # Nothing to change: return the current row unchanged.
            return _channel_dict(current)

        row = (
            await session.execute(
                text(
                    f"UPDATE alert_channels SET {', '.join(assignments)} "  # noqa: S608 - assignments are fixed column literals, not user input
                    "WHERE id = :cid "
                    "RETURNING id, type, project_id, config, enabled, created_at"
                ),
                params,
            )
        ).one()
    return _channel_dict(row)


async def delete_channel(
    org_id: uuid.UUID,
    channel_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> bool:
    """Delete a channel. Return True if one was deleted.

    RLS scopes the DELETE to ``org_id``, so a channel in another org is not
    visible and the delete affects zero rows (reported as a 404, not a
    cross-tenant delete).
    """
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        result = await session.execute(
            text("DELETE FROM alert_channels WHERE id = :cid"),
            {"cid": str(channel_id)},
        )
    return result.rowcount > 0
