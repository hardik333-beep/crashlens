"""Ingest hot-path helpers: DSN resolution, gzip decompression, envelope validation.

These functions are deliberately kept free of framework state where possible so
the decompression cap and the envelope validation matrix are unit-testable
without a live Redis, Postgres, or ASGI app. The route handler in
``app/routes/ingest.py`` composes them with the Redis token bucket
(``app/ratelimit.py``) and the arq enqueue.

SECRETS / PII HYGIENE: nothing here logs a DSN key, a payload, or a user stack
trace. Every validation failure raises with a GENERIC message; the route maps
that to a generic HTTP body so a rejection never reflects attacker-supplied
payload content back to the client.

DSN RESOLUTION SESSION: :func:`resolve_dsn_key` reads ``dsn_keys`` through
``system_session`` (app/db.py bootstrap flow 4), the ONLY sanctioned cross-tenant
lookup for the hot path. It runs BEFORE any org context exists, learns which
project/org an event belongs to, and reads nothing else.
"""

import datetime
import json
import uuid
import zlib
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import system_session

# 1 MB decompressed-size cap (docs/PROTOCOL.md section 1 and ruling 1). Enforced
# INCREMENTALLY during decompression so a gzip bomb is cut off mid-stream and is
# never fully expanded in memory.
MAX_DECOMPRESSED_BYTES = 1024 * 1024

# Output-buffer chunk for the incremental decompress loop.
_DECOMPRESS_CHUNK = 64 * 1024

# The five accepted event levels (docs/PROTOCOL.md section 3.1).
VALID_LEVELS = frozenset({"fatal", "error", "warning", "info", "debug"})


class IngestError(Exception):
    """Base for ingest rejections."""


class PayloadTooLarge(IngestError):
    """The decompressed body exceeded ``MAX_DECOMPRESSED_BYTES`` (maps to 413)."""


class MalformedBody(IngestError):
    """The body could not be decompressed or parsed as JSON (maps to 400)."""


class InvalidEnvelope(IngestError):
    """The JSON parsed but failed required-shape validation (maps to 400)."""


@dataclass
class DsnKeyRecord:
    """The fields of a resolved DSN key needed to authorise and route an event."""

    id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID
    status: str


def decompress_gzip(data: bytes) -> bytes:
    """Return gzip-decompressed ``data``, enforcing the 1 MB cap INCREMENTALLY.

    Uses ``zlib.decompressobj`` with ``wbits=47`` (automatic gzip/zlib header
    detection) and passes ``max_length`` to each ``decompress`` call so the
    output buffer grows one bounded chunk at a time. The running total is checked
    after every chunk, so a decompression bomb is rejected with
    ``PayloadTooLarge`` the moment it crosses the cap WITHOUT expanding the rest
    of the stream (the decompressor stops producing output once ``max_length`` is
    reached and parks the remaining input in ``unconsumed_tail``). A corrupt gzip
    stream raises ``MalformedBody``.
    """
    decompressor = zlib.decompressobj(47)
    out = bytearray()
    try:
        chunk = decompressor.decompress(data, _DECOMPRESS_CHUNK)
        while chunk:
            out.extend(chunk)
            if len(out) > MAX_DECOMPRESSED_BYTES:
                raise PayloadTooLarge("decompressed body exceeds cap")
            # unconsumed_tail holds input deferred because output hit max_length;
            # keep draining it a bounded chunk at a time.
            chunk = decompressor.decompress(
                decompressor.unconsumed_tail, _DECOMPRESS_CHUNK
            )
        tail = decompressor.flush()
        if tail:
            out.extend(tail)
            if len(out) > MAX_DECOMPRESSED_BYTES:
                raise PayloadTooLarge("decompressed body exceeds cap")
    except zlib.error as exc:
        raise MalformedBody("malformed gzip body") from exc
    return bytes(out)


def parse_json(body: bytes) -> dict:
    """Parse ``body`` as a JSON object. Raise on malformed input or a non-object."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise MalformedBody("body is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise InvalidEnvelope("event must be a JSON object")
    return parsed


def _require_nonempty_str(envelope: dict, field: str) -> None:
    value = envelope.get(field)
    if not isinstance(value, str) or not value:
        raise InvalidEnvelope(f"missing or invalid field: {field}")


def _is_rfc3339(value: str) -> bool:
    """Return True if ``value`` parses as an RFC3339 / ISO 8601 timestamp."""
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return True


def validate_envelope(envelope: dict) -> None:
    """Validate ONLY the required shape (docs/PROTOCOL.md 3.1, 3.2); else raise.

    Required: ``event_id`` (UUID string), ``timestamp`` (RFC3339 string),
    ``platform`` (non-empty string), ``level`` (one of the five), ``environment``
    (non-empty string), ``sdk`` (object with string ``name`` and ``version``),
    and at least one of ``message`` (string) or ``exception`` (object). Unknown
    top-level fields are left UNTOUCHED and pass through (forward compatible).
    Nothing else is validated here: string truncation, frame limits, breadcrumb
    trimming, and chained-exception depth are the async worker's job.
    """
    event_id = envelope.get("event_id")
    if not isinstance(event_id, str):
        raise InvalidEnvelope("missing or invalid field: event_id")
    try:
        uuid.UUID(event_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidEnvelope("missing or invalid field: event_id") from exc

    timestamp = envelope.get("timestamp")
    if not isinstance(timestamp, str) or not _is_rfc3339(timestamp):
        raise InvalidEnvelope("missing or invalid field: timestamp")

    _require_nonempty_str(envelope, "platform")

    if envelope.get("level") not in VALID_LEVELS:
        raise InvalidEnvelope("missing or invalid field: level")

    _require_nonempty_str(envelope, "environment")

    sdk = envelope.get("sdk")
    if not isinstance(sdk, dict):
        raise InvalidEnvelope("missing or invalid field: sdk")
    if not isinstance(sdk.get("name"), str) or not sdk.get("name"):
        raise InvalidEnvelope("missing or invalid field: sdk.name")
    if not isinstance(sdk.get("version"), str) or not sdk.get("version"):
        raise InvalidEnvelope("missing or invalid field: sdk.version")

    message = envelope.get("message")
    exception = envelope.get("exception")
    if message is None and exception is None:
        raise InvalidEnvelope("event requires a message or an exception")
    if message is not None and not isinstance(message, str):
        raise InvalidEnvelope("invalid field: message")
    if exception is not None and not isinstance(exception, dict):
        raise InvalidEnvelope("invalid field: exception")


async def resolve_dsn_key(
    public_key: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> DsnKeyRecord | None:
    """Return the DSN key row for ``public_key`` or None if no such key exists.

    Reads ``dsn_keys`` through ``system_session`` (the sanctioned BYPASSRLS hot
    path lookup). The status is returned as-is; the caller decides that a
    non-``active`` key is unauthorised. ``session_factory`` is injectable for
    tests; production omits it. The key itself is never logged here or by callers.
    """
    async with system_session(session_factory=session_factory) as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, org_id, project_id, status "
                    "FROM dsn_keys WHERE public_key = :pk"
                ),
                {"pk": public_key},
            )
        ).one_or_none()
    if row is None:
        return None
    return DsnKeyRecord(
        id=row.id,
        org_id=row.org_id,
        project_id=row.project_id,
        status=row.status,
    )
