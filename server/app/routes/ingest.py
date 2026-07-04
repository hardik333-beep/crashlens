"""Public event ingest endpoint (docs/PROTOCOL.md).

``POST /ingest/{project_id}/`` -- mounted without an ``/api`` prefix because the
reverse proxy strips it, so the browser/SDK path ``POST /api/ingest/{id}/``
reaches the app here. This is the ONE public, high-volume endpoint and is NOT
protected by a session JWT: it authenticates with the DSN public key in the
``X-Crashlens-Key`` header.

The handler does the minimum on the hot path and defers everything expensive:

1. Authenticate the DSN key (``system_session`` lookup); reject 401/403.
2. Rate-limit per DSN key via a Redis token bucket (429 + Retry-After),
   BEFORE the body is read. Every authenticated request consumes a token
   whether or not its payload is valid, so a client cannot burn server CPU
   (gzip decompression up to 1 MB, JSON parse) on an unthrottled path by
   sending deliberately invalid payloads; a 429 is decided before the body is
   even read.
3. Apply per-project sampling (W6-04), AFTER the rate limit and BEFORE the
   body is read: if the resolved project's ``sampling_rate`` is below 1.0 and
   the roll misses, return 202 with a null ``id`` WITHOUT reading the body or
   enqueueing. Per docs/PROTOCOL.md, a 202 acceptance does not guarantee an
   event's survival; a sampled-out event is the documented case.
4. Read the body, decompressing gzip under a HARD incremental 1 MB cap (413).
5. Parse and shape-validate the envelope (400, generic body).
6. Enqueue the raw validated envelope plus server-derived routing metadata to
   the arq ``process_event`` job and return 202 immediately.

It performs NO grouping and NO Postgres writes inline, and it NEVER logs the DSN
key, the payload, or a user stack trace -- only the project id, a decision class,
and byte sizes.
"""

import asyncio
import datetime
import logging
import random
import time
import uuid
from collections.abc import Callable

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from app import ingest
from app.ratelimit import check_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ingest"])

# Injectable sampling coin-flip. Module-level so tests can monkeypatch
# ``app.routes.ingest.rng`` for a deterministic sequence; production leaves it
# at the default ``random.random`` (uniform on [0.0, 1.0)).
rng: Callable[[], float] = random.random

# arq job the async processor will register in a later slice. Enqueuing to a
# not-yet-registered function name is valid in arq. FLAGGED (governor review):
# the job name is a contract between this producer and that future consumer.
PROCESS_EVENT_JOB = "process_event"

# Guards lazy creation of the single shared arq Redis pool.
_pool_lock = asyncio.Lock()

# Generic rejection bodies. They never echo payload content, so a malformed or
# unauthorised request cannot reflect attacker input back.
_DETAILS: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "Invalid event payload.",
    status.HTTP_401_UNAUTHORIZED: "Unauthorized.",
    status.HTTP_403_FORBIDDEN: "Forbidden.",
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: "Payload too large.",
    status.HTTP_429_TOO_MANY_REQUESTS: "Too many requests.",
}


def _log_decision(
    project_id: uuid.UUID, decision: str, *, size: int | None = None
) -> None:
    """Log only the project id, the decision class, and a byte size.

    Deliberately excludes the DSN key, the payload, and any user stack trace.
    """
    logger.info(
        "ingest project_id=%s decision=%s size=%s", project_id, decision, size
    )


def _reject(
    project_id: uuid.UUID,
    decision: str,
    status_code: int,
    *,
    size: int | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    """Build a uniform generic rejection response and log the decision.

    Returned (not raised) so the call site keeps an explicit ``return``, which
    makes the linear reject-or-continue control flow obvious.
    """
    _log_decision(project_id, decision, size=size)
    return JSONResponse(
        status_code=status_code,
        content={"detail": _DETAILS[status_code]},
        headers=headers,
    )


def _sampled_out(sampling_rate: float) -> bool:
    """Return True if an event for a project at ``sampling_rate`` should drop.

    A rate of 1.0 (the "keep every event" default) short-circuits to False
    WITHOUT calling ``rng`` at all, so the common case never rolls the dice.
    Otherwise the roll drops the event when ``rng() >= sampling_rate``: since
    ``rng`` is uniform on [0.0, 1.0), P(rng() < sampling_rate) == sampling_rate,
    so this keeps exactly ``sampling_rate`` of events on average, including the
    rate == 0.0 boundary (every roll is >= 0.0, so every event drops).
    """
    if sampling_rate >= 1.0:
        return False
    return rng() >= sampling_rate


async def get_ingest_pool(app) -> ArqRedis:  # noqa: ANN001 - FastAPI app object
    """Return the process-wide arq Redis pool, creating it once on first use.

    One shared pool serves BOTH the rate-limit ``eval`` and the ``enqueue_job``
    for every request (never a pool per request). Creation is lazy and
    double-checked under a lock so a burst of concurrent first requests makes
    exactly one pool. The pool is closed by the app lifespan on shutdown.
    """
    pool = getattr(app.state, "arq_pool", None)
    if pool is not None:
        return pool
    async with _pool_lock:
        pool = getattr(app.state, "arq_pool", None)
        if pool is None:
            pool = await create_pool(
                RedisSettings.from_dsn(app.state.settings.redis_url)
            )
            app.state.arq_pool = pool
    return pool


@router.post("/ingest/{project_id}/")
async def ingest_event(project_id: uuid.UUID, request: Request) -> Response:
    """Accept an event for ``project_id``, or reject with a generic error body.

    A 202 acceptance does not guarantee an event's survival: a sampled-out
    event (the project's per-project sampling_rate rolled a drop) is the
    documented case where the body is never read and nothing is enqueued.
    """
    # 1. Authenticate the DSN key. Never logged.
    public_key = request.headers.get("x-crashlens-key")
    if not public_key:
        return _reject(project_id, "missing_key", status.HTTP_401_UNAUTHORIZED)

    record = await ingest.resolve_dsn_key(public_key)
    if record is None or record.status != "active":
        return _reject(project_id, "invalid_key", status.HTTP_401_UNAUTHORIZED)
    if str(record.project_id) != str(project_id):
        return _reject(project_id, "project_mismatch", status.HTTP_403_FORBIDDEN)

    # 2. Rate-limit per DSN key (AFTER auth, BEFORE the body is read), atomically
    # in Redis. The token is spent whether or not the payload turns out to be
    # valid, so invalid payloads cannot burn decompression/parse CPU unthrottled.
    pool = await get_ingest_pool(request.app)
    decision = await check_rate_limit(pool, record.id, time.time())
    if not decision.allowed:
        return _reject(
            project_id,
            "rate_limited",
            status.HTTP_429_TOO_MANY_REQUESTS,
            size=None,
            headers={"Retry-After": str(decision.retry_after)},
        )

    # 3. Apply per-project sampling (AFTER rate limiting, BEFORE the body is
    # read). A sampled-out event is accepted (202) but never read or enqueued;
    # docs/PROTOCOL.md: 202 does not guarantee survival, and this is the
    # documented case.
    if _sampled_out(record.sampling_rate):
        _log_decision(project_id, "sampled")
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED, content={"id": None}
        )

    # 4. Read the body, decompressing gzip under the incremental 1 MB cap.
    body = await request.body()
    wire_size = len(body)
    encoding = request.headers.get("content-encoding", "").lower()
    if "gzip" in encoding:
        try:
            body = ingest.decompress_gzip(body)
        except ingest.PayloadTooLarge:
            return _reject(
                project_id,
                "too_large",
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                size=wire_size,
            )
        except ingest.MalformedBody:
            return _reject(
                project_id, "malformed_gzip", status.HTTP_400_BAD_REQUEST, size=wire_size
            )
    elif len(body) > ingest.MAX_DECOMPRESSED_BYTES:
        # Defence in depth: the edge caps compressed bytes, but an identity body
        # over the cap is rejected here too.
        return _reject(
            project_id,
            "too_large",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            size=wire_size,
        )

    # 5. Parse and shape-validate. Any failure is a generic 400.
    try:
        envelope = ingest.parse_json(body)
        ingest.validate_envelope(envelope)
    except ingest.MalformedBody:
        return _reject(
            project_id, "malformed_json", status.HTTP_400_BAD_REQUEST, size=len(body)
        )
    except ingest.InvalidEnvelope:
        return _reject(
            project_id, "invalid_envelope", status.HTTP_400_BAD_REQUEST, size=len(body)
        )

    # 6. Enqueue for async processing and return 202 immediately. Server-derived
    # routing metadata is added here; the client can only influence the envelope.
    await pool.enqueue_job(
        PROCESS_EVENT_JOB,
        envelope=envelope,
        org_id=str(record.org_id),
        project_id=str(record.project_id),
        dsn_key_id=str(record.id),
        received_at=datetime.datetime.now(datetime.UTC).isoformat(),
    )
    _log_decision(project_id, "accepted", size=len(body))
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED, content={"id": envelope["event_id"]}
    )
