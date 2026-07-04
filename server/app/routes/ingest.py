"""Public event ingest endpoint (docs/PROTOCOL.md).

``POST /ingest/{project_id}/`` -- mounted without an ``/api`` prefix because the
reverse proxy strips it, so the browser/SDK path ``POST /api/ingest/{id}/``
reaches the app here. This is the ONE public, high-volume endpoint and is NOT
protected by a session JWT: it authenticates with the DSN public key in the
``X-Crashlens-Key`` header.

The handler does the minimum on the hot path and defers everything expensive:

1. Authenticate the DSN key (``system_session`` lookup); reject 401/403.
2. Read the body, decompressing gzip under a HARD incremental 1 MB cap (413).
3. Parse and shape-validate the envelope (400, generic body).
4. Rate-limit per DSN key via a Redis token bucket (429 + Retry-After).
5. Enqueue the raw validated envelope plus server-derived routing metadata to
   the arq ``process_event`` job and return 202 immediately.

It performs NO grouping and NO Postgres writes inline, and it NEVER logs the DSN
key, the payload, or a user stack trace -- only the project id, a decision class,
and byte sizes.
"""

import asyncio
import datetime
import logging
import time
import uuid

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from app import ingest
from app.ratelimit import check_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ingest"])

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
    # 1. Authenticate the DSN key. Never logged.
    public_key = request.headers.get("x-crashlens-key")
    if not public_key:
        return _reject(project_id, "missing_key", status.HTTP_401_UNAUTHORIZED)

    record = await ingest.resolve_dsn_key(public_key)
    if record is None or record.status != "active":
        return _reject(project_id, "invalid_key", status.HTTP_401_UNAUTHORIZED)
    if str(record.project_id) != str(project_id):
        return _reject(project_id, "project_mismatch", status.HTTP_403_FORBIDDEN)

    # 2. Read the body, decompressing gzip under the incremental 1 MB cap.
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

    # 3. Parse and shape-validate. Any failure is a generic 400.
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

    # 4. Rate-limit per DSN key (AFTER auth), atomically in Redis.
    pool = await get_ingest_pool(request.app)
    decision = await check_rate_limit(pool, record.id, time.time())
    if not decision.allowed:
        return _reject(
            project_id,
            "rate_limited",
            status.HTTP_429_TOO_MANY_REQUESTS,
            size=len(body),
            headers={"Retry-After": str(decision.retry_after)},
        )

    # 5. Enqueue for async processing and return 202 immediately. Server-derived
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
