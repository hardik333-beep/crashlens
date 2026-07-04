"""Async event processor: the arq job that turns an ingested envelope into an Issue.

The ingest hot path (``app/routes/ingest.py``) does the cheap work -- authenticate,
rate-limit, shape-validate -- and enqueues the raw validated envelope plus
server-derived routing metadata to this job. Everything expensive happens here,
off the request path:

1. IDEMPOTENCY. Inside ``tenant_session(org_id)`` (RLS scope applied as the
   transaction's first statement, BEFORE any row is loaded -- the hard worker
   gate), check whether ``(project_id, event_id)`` already exists. arq only
   deduplicates by job id when the producer passes ``_job_id``; the ingest route
   deliberately does NOT (a client resend is a new job), so this explicit
   ``SELECT`` is the real idempotency guard. The events PRIMARY KEY
   ``(project_id, event_id, received_at)`` only makes a row unique per exact
   timestamp, so it cannot dedupe a resend that arrives with a fresh
   ``received_at``; the ``SELECT`` closes that gap. A residual TOCTOU race
   between two truly-concurrent processings of the same ``event_id`` remains
   possible and is accepted: it is bounded to at most one duplicate row and
   resends are rare and effectively serialized.

2. NORMALIZE / TRUNCATE the envelope per the FROZEN v1 protocol (docs/PROTOCOL.md
   section 4). Pure, unit-tested functions. Unknown fields pass through untouched.

3. FINGERPRINT the ROOT cause (deepest exception in the cause chain) into a stable
   sha256 hex. Pure, unit-tested.

4. UPSERT the Issue and INSERT the event row in ONE transaction (atomic): a new
   fingerprint creates an Issue; a repeat increments its counter and advances
   ``last_seen``. A new event on a ``resolved`` Issue flips it to ``regressed``
   (base regression behaviour; release-aware regression is a later slice);
   ``ignored`` stays ``ignored``.

SECRETS / PII HYGIENE: this module logs ONLY ids and counters. It NEVER logs
payload contents, messages, tag values, or stack frames.
"""

import copy
import datetime
import hashlib
import json
import logging
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import tenant_session

logger = logging.getLogger(__name__)

# --- Server-side truncation / cap limits (docs/PROTOCOL.md section 4) ----------
MESSAGE_MAX = 8192
TAG_KEY_MAX = 32
TAG_VALUE_MAX = 200
FILENAME_MAX = 256
FUNCTION_MAX = 256
CONTEXT_LINE_MAX = 256
MAX_BREADCRUMBS = 100  # keep the NEWEST 100 (assumed newest-last; FLAGGED)
MAX_FRAMES = 128  # keep the LAST 128 (nearest the crash under canonical order)
MAX_CAUSE_DEPTH = 5  # keep exceptions 1..5; drop the cause link on the 5th
TRUNCATION_MARKER = "..."

# --- Derived-field limits (FLAGGED DEFAULTS; governor review) ------------------
TITLE_MAX = 200
FINGERPRINT_FRAME_LIMIT = 8  # last 8 in_app frames of the root cause

# --- Message-normalization placeholders (FLAGGED DEFAULT; governor review) -----
# So "user 123 not found" and "user 456 not found" fingerprint to one Issue.
# Order matters: UUIDs, then hex (0x-prefixed or long bare hex runs), then any
# remaining digit runs. The bare-hex threshold is deliberately high (16+) so
# ordinary short numbers are handled by the digit rule, not misread as hex.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_HEX_RE = re.compile(r"\b0[xX][0-9a-fA-F]+\b|\b[0-9a-fA-F]{16,}\b")
_NUM_RE = re.compile(r"\d+")


# ==============================================================================
# Pure functions (no I/O; unit-tested without any service).
# ==============================================================================


def truncate_string(value: str, limit: int, marker: str = TRUNCATION_MARKER) -> str:
    """Return ``value`` capped to ``limit`` characters with a trailing ``marker``.

    FLAGGED DEFAULT (marker accounting): when truncation happens the RESULT is
    exactly ``limit`` characters INCLUDING the marker (``value[:limit - len(marker)]
    + marker``), so the stored string never exceeds the protocol cap. Non-string
    input is returned unchanged (defensive: callers only pass strings, but a
    forward-compatible unknown shape must not crash the worker).
    """
    if not isinstance(value, str) or len(value) <= limit:
        return value
    keep = max(limit - len(marker), 0)
    return value[:keep] + marker


def normalize_message(message: str) -> str:
    """Collapse volatile tokens (UUIDs, hex ids, digit runs) to stable placeholders.

    Used only for the exception-less fingerprint so structurally identical log
    messages that differ only by an id/number group together.
    """
    if not isinstance(message, str):
        return ""
    collapsed = _UUID_RE.sub("<uuid>", message)
    collapsed = _HEX_RE.sub("<hex>", collapsed)
    collapsed = _NUM_RE.sub("<n>", collapsed)
    return collapsed


def _normalize_frame(frame: dict) -> dict:
    """Truncate the string fields of one stack frame; leave everything else intact."""
    if not isinstance(frame, dict):
        return frame
    out = dict(frame)
    if isinstance(out.get("filename"), str):
        out["filename"] = truncate_string(out["filename"], FILENAME_MAX)
    if isinstance(out.get("function"), str):
        out["function"] = truncate_string(out["function"], FUNCTION_MAX)
    if isinstance(out.get("context_line"), str):
        out["context_line"] = truncate_string(out["context_line"], CONTEXT_LINE_MAX)
    for key in ("pre_context", "post_context"):
        lines = out.get(key)
        if isinstance(lines, list):
            out[key] = [
                truncate_string(line, CONTEXT_LINE_MAX) if isinstance(line, str) else line
                for line in lines
            ]
    return out


def _normalize_exception(exception: dict, depth: int = 1) -> dict:
    """Normalize one exception in the chain: cap frames, truncate frame strings, bound cause depth.

    ``depth`` is 1 for the top (presented) exception. Frames are capped to the
    LAST ``MAX_FRAMES``. The recursive ``cause`` is followed only while
    ``depth < MAX_CAUSE_DEPTH``; at the maximum depth the ``cause`` link is
    dropped (deeper chains truncated server-side, per protocol ruling 6).
    """
    if not isinstance(exception, dict):
        return exception
    out = dict(exception)
    stacktrace = out.get("stacktrace")
    if isinstance(stacktrace, dict):
        frames = stacktrace.get("frames")
        if isinstance(frames, list):
            capped = frames[-MAX_FRAMES:]
            out["stacktrace"] = {
                **stacktrace,
                "frames": [_normalize_frame(frame) for frame in capped],
            }
    cause = out.get("cause")
    if isinstance(cause, dict) and depth < MAX_CAUSE_DEPTH:
        out["cause"] = _normalize_exception(cause, depth + 1)
    elif "cause" in out:
        # At max depth (or a non-dict cause): truncate the chain here.
        out.pop("cause", None)
    return out


def normalize_envelope(envelope: dict) -> dict:
    """Return a truncated/normalized DEEP COPY of ``envelope`` per the frozen protocol.

    Applies every server-side rule in docs/PROTOCOL.md section 4 (message cap,
    tag key/value caps, frame string caps, frame LAST-128 cap, breadcrumb
    NEWEST-100 cap, cause-chain depth 5). The input dict is never mutated.
    Unknown fields are preserved untouched (forward compatible).
    """
    env = copy.deepcopy(envelope)

    if isinstance(env.get("message"), str):
        env["message"] = truncate_string(env["message"], MESSAGE_MAX)

    tags = env.get("tags")
    if isinstance(tags, dict):
        env["tags"] = {
            (truncate_string(key, TAG_KEY_MAX) if isinstance(key, str) else key): (
                truncate_string(value, TAG_VALUE_MAX) if isinstance(value, str) else value
            )
            for key, value in tags.items()
        }

    breadcrumbs = env.get("breadcrumbs")
    if isinstance(breadcrumbs, list):
        env["breadcrumbs"] = breadcrumbs[-MAX_BREADCRUMBS:]

    exception = env.get("exception")
    if isinstance(exception, dict):
        env["exception"] = _normalize_exception(exception)

    return env


def walk_to_root_cause(exception: dict) -> dict:
    """Return the deepest exception in the ``cause`` chain (the root cause).

    Bounded by ``MAX_CAUSE_DEPTH`` iterations as a defensive stop even though
    :func:`normalize_envelope` already truncated the chain.
    """
    current = exception
    for _ in range(MAX_CAUSE_DEPTH):
        cause = current.get("cause") if isinstance(current, dict) else None
        if not isinstance(cause, dict):
            break
        current = cause
    return current


def compute_fingerprint(normalized: dict) -> str:
    """Return a stable sha256 hex fingerprint for grouping ``normalized`` into an Issue.

    With an exception: hash the ROOT cause's type plus the (filename, function)
    of its last ``FINGERPRINT_FRAME_LIMIT`` frames, selected in THREE tiers
    (governor ruling, W2-02/03 review):

    1. ``in_app`` frames (``in_app`` missing defaults to true), when any exist;
    2. otherwise ALL frames -- an all-library stack (e.g. a DB driver timeout
       raised entirely inside a client library) still discriminates by WHERE in
       the library it failed, instead of collapsing to type+platform and
       over-grouping unrelated failures;
    3. only when the root cause has NO frames at all does the signature
       legitimately reduce to type + platform.

    Without an exception: hash the level plus the normalized message.
    ``platform`` is always part of the hash input so the same message on
    different platforms does not collide.
    """
    platform = normalized.get("platform")
    exception = normalized.get("exception")

    if isinstance(exception, dict):
        root = walk_to_root_cause(exception)
        stacktrace = root.get("stacktrace") if isinstance(root, dict) else None
        frames = stacktrace.get("frames", []) if isinstance(stacktrace, dict) else []
        dict_frames = [frame for frame in frames if isinstance(frame, dict)]
        in_app_frames = [
            frame for frame in dict_frames if frame.get("in_app", True)
        ]
        # Tier 2 fallback: frames exist but none are in_app (all-library
        # stack) -> use all frames rather than hashing an empty frame list.
        candidate_frames = in_app_frames if in_app_frames else dict_frames
        selected = candidate_frames[-FINGERPRINT_FRAME_LIMIT:]
        signature = {
            "v": 1,
            "platform": platform,
            "type": root.get("type") if isinstance(root, dict) else None,
            "frames": [[f.get("filename"), f.get("function")] for f in selected],
        }
    else:
        signature = {
            "v": 1,
            "platform": platform,
            "level": normalized.get("level"),
            "message": normalize_message(normalized.get("message") or ""),
        }

    serialized = json.dumps(
        signature, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def derive_title(normalized: dict) -> str:
    """Return the Issue title: ``"Type: value-first-line"`` or the message first line.

    Uses the TOP-LEVEL (presented) exception's type/value -- the error the user
    sees -- not the root cause used for fingerprinting. Capped to ``TITLE_MAX``.
    """
    exception = normalized.get("exception")
    if isinstance(exception, dict):
        exc_type = exception.get("type") or "Error"
        value = exception.get("value")
        first_line = value.splitlines()[0] if isinstance(value, str) and value else ""
        title = f"{exc_type}: {first_line}" if first_line else str(exc_type)
    else:
        message = normalized.get("message")
        title = message.splitlines()[0] if isinstance(message, str) and message else ""
    return truncate_string(title, TITLE_MAX)


def _coerce_timestamp(value: str) -> datetime.datetime:
    """Parse an RFC3339/ISO timestamp, falling back to now(UTC) if malformed.

    ``received_at`` is server-derived and should always be valid ISO, but a
    malformed timestamp string must never crash processing (defensive): it
    falls back to the current UTC time so the row still lands in a valid
    partition.
    """
    if isinstance(value, str):
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.UTC)
            return parsed
        except ValueError:
            pass
    return datetime.datetime.now(datetime.UTC)


# ==============================================================================
# The upsert. Shipped exactly as below.
# ==============================================================================
#
# A CTE wraps the specified issues upsert so we can report the regression
# TRANSITION precisely: ``prior`` snapshots the pre-upsert status (the CTE terms
# share one snapshot, so it sees the row as it was BEFORE the INSERT ... ON
# CONFLICT ran), letting the caller distinguish "a NEW event flipped a resolved
# Issue to regressed on THIS event" from "an already-regressed Issue got another
# event". ``(xmax = 0)`` is the standard flag for a freshly INSERTed (vs
# UPDATEd) row. ``first_seen`` is set only on INSERT (never in DO UPDATE).
_UPSERT_ISSUE_SQL = text(
    """
    WITH prior AS (
        SELECT id, status
        FROM issues
        WHERE project_id = :project_id AND fingerprint = :fingerprint
    ),
    upserted AS (
        INSERT INTO issues (
            org_id, project_id, fingerprint, title, level, status,
            first_seen, last_seen, event_count
        )
        VALUES (
            :org_id, :project_id, :fingerprint, :title, :level, 'unresolved',
            :seen_at, :seen_at, 1
        )
        ON CONFLICT ON CONSTRAINT uq_issues_project_fingerprint DO UPDATE SET
            last_seen = GREATEST(issues.last_seen, excluded.last_seen),
            event_count = issues.event_count + 1,
            level = excluded.level,
            status = CASE
                WHEN issues.status = 'resolved' THEN 'regressed'
                WHEN issues.status = 'ignored' THEN issues.status
                ELSE issues.status
            END
        RETURNING id, status, event_count, (xmax = 0) AS inserted
    )
    SELECT
        upserted.id,
        upserted.status,
        upserted.event_count,
        upserted.inserted,
        prior.status AS prior_status
    FROM upserted
    LEFT JOIN prior ON prior.id = upserted.id
    """
)

_INSERT_EVENT_SQL = text(
    """
    INSERT INTO events (
        org_id, project_id, issue_id, event_id, received_at,
        environment, release, level, payload
    )
    VALUES (
        :org_id, :project_id, :issue_id, :event_id, :received_at,
        :environment, :release, :level, CAST(:payload AS jsonb)
    )
    """
)

_DEDUPE_EVENT_SQL = text(
    "SELECT 1 FROM events WHERE project_id = :project_id AND event_id = :event_id LIMIT 1"
)


async def process_event(
    ctx: dict,
    *,
    envelope: dict,
    org_id: str,
    project_id: str,
    dsn_key_id: str,
    received_at: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """Consume one ingested envelope: fingerprint, upsert its Issue, store the event.

    Producer contract (``app/routes/ingest.py``): ``envelope`` is the raw
    validated event; ``org_id`` / ``project_id`` / ``dsn_key_id`` are
    server-derived routing ids; ``received_at`` is a server ISO timestamp.

    ``session_factory`` is injectable for tests (to bind the non-superuser,
    RLS-enforcing role), mirroring ``app/jobs/retention.py``; arq calls this with
    only ``ctx`` + the keyword contract, so production uses the default factory.

    Returns a dict signal (consumed by a later alerts slice): ``issue_id``,
    ``created`` (new Issue), ``regressed`` (a resolved Issue flipped on THIS
    event), ``status`` (post-upsert), ``event_count``, and ``event_id``.
    """
    # POISON GUARD: normalization + fingerprinting are pure/CPU and must never
    # retry-loop on an unparseable-but-validated envelope. DB errors below are
    # intentionally NOT caught here so genuine transient failures still retry
    # (WorkerSettings.max_tries).
    event_id = envelope.get("event_id") if isinstance(envelope, dict) else None
    try:
        normalized = normalize_envelope(envelope)
        fingerprint = compute_fingerprint(normalized)
        title = derive_title(normalized)
        event_id = normalized["event_id"]
        level = normalized["level"]
        environment = normalized["environment"]
        release = normalized.get("release")
    except Exception:  # noqa: BLE001 - defensive poison-event boundary
        logger.warning("process_event: dropping poison event event_id=%s", event_id)
        return {"status": "poison", "event_id": event_id, "issue_id": None}

    seen_at = _coerce_timestamp(received_at)
    payload_json = json.dumps(normalized, ensure_ascii=False)

    async with tenant_session(org_id, session_factory=session_factory) as session:
        # 1. IDEMPOTENCY: RLS scope is already the first statement of this
        # transaction (tenant_session), so this read is org-scoped before any
        # row is loaded. Bounded scan across partitions by (project_id, event_id).
        duplicate = (
            await session.execute(
                _DEDUPE_EVENT_SQL,
                {"project_id": project_id, "event_id": event_id},
            )
        ).first()
        if duplicate is not None:
            logger.info(
                "process_event: duplicate event skipped event_id=%s project_id=%s",
                event_id,
                project_id,
            )
            return {"status": "duplicate", "event_id": event_id, "issue_id": None}

        # 2. UPSERT the Issue (atomic with the event insert below).
        row = (
            await session.execute(
                _UPSERT_ISSUE_SQL,
                {
                    "org_id": org_id,
                    "project_id": project_id,
                    "fingerprint": fingerprint,
                    "title": title,
                    "level": level,
                    "seen_at": seen_at,
                },
            )
        ).one()
        issue_id = row.id
        created = bool(row.inserted)
        regressed = (
            not created
            and row.prior_status == "resolved"
            and row.status == "regressed"
        )

        # 3. INSERT the event row in the SAME transaction.
        await session.execute(
            _INSERT_EVENT_SQL,
            {
                "org_id": org_id,
                "project_id": project_id,
                "issue_id": issue_id,
                "event_id": event_id,
                "received_at": seen_at,
                "environment": environment,
                "release": release,
                "level": level,
                "payload": payload_json,
            },
        )

    logger.info(
        "process_event: stored event_id=%s issue_id=%s created=%s regressed=%s "
        "status=%s event_count=%d",
        event_id,
        issue_id,
        created,
        regressed,
        row.status,
        row.event_count,
    )

    # ALERT DISPATCH (post-commit, off the transaction). Only a genuinely NEW
    # Issue or a resolved->regressed TRANSITION is worth alerting on; duplicate
    # and poison paths returned earlier and never reach here. The enqueue is done
    # AFTER the transaction above committed (never inside it, so a slow/unavailable
    # Redis can never hold a DB transaction open or roll back a stored event), via
    # the arq redis pool arq injects into a job's ctx as ctx["redis"] (grounded in
    # arq.worker: self.ctx['redis'] = self.pool). Tests call process_event with a
    # bare ctx and no redis, so a missing pool simply means "no dispatch". A blip
    # enqueuing is logged and swallowed, NOT raised: re-raising would fail the job
    # and arq's retry would re-run process_event, hit the duplicate guard, and
    # return WITHOUT dispatching -- silently losing the alert. FLAGGED for governor
    # review: at-most-once alert delivery (a dropped enqueue loses that one alert).
    if created or regressed:
        pool = ctx.get("redis") if isinstance(ctx, dict) else None
        if pool is not None:
            try:
                await pool.enqueue_job(
                    "dispatch_alerts",
                    org_id=org_id,
                    project_id=project_id,
                    issue_id=str(issue_id),
                    kind="regression" if regressed else "new",
                    title=title,
                    level=level,
                )
            except Exception:  # noqa: BLE001 - a dispatch enqueue blip must not fail a stored event
                logger.warning(
                    "process_event: failed to enqueue dispatch_alerts issue_id=%s",
                    issue_id,
                )

    return {
        "status": row.status,
        "event_id": event_id,
        "issue_id": str(issue_id),
        "created": created,
        "regressed": regressed,
        "event_count": row.event_count,
    }
