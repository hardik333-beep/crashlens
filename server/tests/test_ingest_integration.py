"""Integration tests for the public ingest endpoint (marked ``db``).

These exercise the endpoint end to end through the ASGI app and require BOTH a
live PostgreSQL (for the ``system_session`` DSN lookup) and a live Redis (for
the token bucket and the arq enqueue). They SKIP cleanly when either is
unreachable, so ``pytest -q`` passes locally without services.

CI NOTE (flagged for the governor): the CI ``test`` job currently provisions a
postgres service but -- at the time this slice was written -- NOT a redis
service. Until a ``redis:7`` service is added to ``.github/workflows/ci.yml``
(owned by the parallel CI agent, not edited here), these tests SKIP in CI rather
than run. The skip is driven by the redis reachability probe below.

Coverage: happy-path 202 with the job landing in the arq queue, 401 for an
unknown and a revoked key, 403 for a project mismatch, 413 for an oversize body,
and 429 with a Retry-After header once the bucket is exhausted.
"""

import json
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis
from arq import create_pool
from arq.connections import RedisSettings
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import ratelimit, security
from app.config import get_settings
from app.main import create_app
from app.routes.ingest import PROCESS_EVENT_JOB

pytestmark = pytest.mark.db


# --- Fixtures -----------------------------------------------------------------
@pytest_asyncio.fixture
async def superuser_engine():
    """Engine on the migration/superuser DATABASE_URL. Skips if unreachable."""
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("PostgreSQL not reachable; skipping ingest integration tests")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def redis_client():
    """Redis client for the test DB. Skips if unreachable; flushes for isolation."""
    client = redis.from_url(get_settings().redis_url)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis not reachable; skipping ingest integration tests")
    # A clean slate so the arq queue and rate-limit buckets do not leak between
    # tests. This targets the configured test Redis DB only.
    await client.flushdb()
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --- Seed helpers -------------------------------------------------------------
async def _seed_org(conn, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO orgs (id, name, slug) VALUES (:id, :name, :slug)"),
        {"id": org_id, "name": name, "slug": f"{name}-{org_id}"},
    )
    return org_id


async def _seed_project(conn, org_id: uuid.UUID, name: str) -> uuid.UUID:
    project_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO projects (id, org_id, name, slug) "
            "VALUES (:id, :oid, :name, :slug)"
        ),
        {"id": project_id, "oid": org_id, "name": name, "slug": f"{name}-{project_id}"},
    )
    return project_id


async def _seed_key(
    conn, org_id: uuid.UUID, project_id: uuid.UUID, status: str = "active"
) -> tuple[uuid.UUID, str]:
    key_id = uuid.uuid4()
    public_key = security.generate_public_key()
    await conn.execute(
        text(
            "INSERT INTO dsn_keys (id, org_id, project_id, public_key, status) "
            "VALUES (:id, :oid, :pid, :pk, :status)"
        ),
        {
            "id": key_id,
            "oid": org_id,
            "pid": project_id,
            "pk": public_key,
            "status": status,
        },
    )
    return key_id, public_key


def _valid_envelope() -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "timestamp": "2026-07-04T12:00:00.000Z",
        "platform": "python",
        "level": "error",
        "message": "Division by zero in invoice total",
        "environment": "production",
        "sdk": {"name": "crashlens-python", "version": "0.1.0"},
    }


async def _queued_jobs():
    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        return await pool.queued_jobs()
    finally:
        await pool.aclose()


# --- Tests --------------------------------------------------------------------
async def test_happy_path_202_and_job_lands_in_queue(
    superuser_engine, redis_client, client
) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "IngestCo")
        project_id = await _seed_project(conn, org_id, "web")
        key_id, public_key = await _seed_key(conn, org_id, project_id)
    try:
        envelope = _valid_envelope()
        resp = await client.post(
            f"/ingest/{project_id}/",
            headers={"X-Crashlens-Key": public_key},
            content=json.dumps(envelope),
        )
        assert resp.status_code == 202
        assert resp.json() == {"id": envelope["event_id"]}

        # The job actually landed on the arq queue with server-derived routing.
        jobs = await _queued_jobs()
        matching = [j for j in jobs if j.function == PROCESS_EVENT_JOB]
        assert len(matching) == 1
        kwargs = matching[0].kwargs
        assert kwargs["envelope"]["event_id"] == envelope["event_id"]
        assert kwargs["org_id"] == str(org_id)
        assert kwargs["project_id"] == str(project_id)
        assert kwargs["dsn_key_id"] == str(key_id)
        assert "received_at" in kwargs
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})


async def test_gzip_happy_path_202(superuser_engine, redis_client, client) -> None:
    import gzip

    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "GzipCo")
        project_id = await _seed_project(conn, org_id, "web")
        _key_id, public_key = await _seed_key(conn, org_id, project_id)
    try:
        envelope = _valid_envelope()
        resp = await client.post(
            f"/ingest/{project_id}/",
            headers={
                "X-Crashlens-Key": public_key,
                "Content-Encoding": "gzip",
            },
            content=gzip.compress(json.dumps(envelope).encode()),
        )
        assert resp.status_code == 202
        assert resp.json() == {"id": envelope["event_id"]}
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})


async def test_missing_key_401(superuser_engine, redis_client, client) -> None:
    resp = await client.post(
        f"/ingest/{uuid.uuid4()}/", content=json.dumps(_valid_envelope())
    )
    assert resp.status_code == 401


async def test_unknown_key_401(superuser_engine, redis_client, client) -> None:
    resp = await client.post(
        f"/ingest/{uuid.uuid4()}/",
        headers={"X-Crashlens-Key": security.generate_public_key()},
        content=json.dumps(_valid_envelope()),
    )
    assert resp.status_code == 401


async def test_revoked_key_401(superuser_engine, redis_client, client) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "RevokedCo")
        project_id = await _seed_project(conn, org_id, "web")
        _key_id, public_key = await _seed_key(conn, org_id, project_id, status="revoked")
    try:
        resp = await client.post(
            f"/ingest/{project_id}/",
            headers={"X-Crashlens-Key": public_key},
            content=json.dumps(_valid_envelope()),
        )
        assert resp.status_code == 401
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})


async def test_project_mismatch_403(superuser_engine, redis_client, client) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "MismatchCo")
        project_a = await _seed_project(conn, org_id, "a")
        project_b = await _seed_project(conn, org_id, "b")
        _key_id, key_a = await _seed_key(conn, org_id, project_a)
    try:
        # A valid, active key for project A used against project B's path.
        resp = await client.post(
            f"/ingest/{project_b}/",
            headers={"X-Crashlens-Key": key_a},
            content=json.dumps(_valid_envelope()),
        )
        assert resp.status_code == 403
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})


async def test_oversize_body_413(superuser_engine, redis_client, client) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "OversizeCo")
        project_id = await _seed_project(conn, org_id, "web")
        _key_id, public_key = await _seed_key(conn, org_id, project_id)
    try:
        oversized = b'{"event_id": "' + b"a" * (1024 * 1024 + 16) + b'"}'
        resp = await client.post(
            f"/ingest/{project_id}/",
            headers={"X-Crashlens-Key": public_key},
            content=oversized,
        )
        assert resp.status_code == 413
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})


async def test_invalid_envelope_400(superuser_engine, redis_client, client) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "BadShapeCo")
        project_id = await _seed_project(conn, org_id, "web")
        _key_id, public_key = await _seed_key(conn, org_id, project_id)
    try:
        envelope = _valid_envelope()
        del envelope["level"]  # required field missing
        resp = await client.post(
            f"/ingest/{project_id}/",
            headers={"X-Crashlens-Key": public_key},
            content=json.dumps(envelope),
        )
        assert resp.status_code == 400
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})


async def test_rate_limit_429_with_retry_after(
    superuser_engine, redis_client, client
) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "RateCo")
        project_id = await _seed_project(conn, org_id, "web")
        key_id, public_key = await _seed_key(conn, org_id, project_id)
    try:
        # Pre-drain the bucket for this DSN key to zero tokens, timestamped now,
        # so the very next request is denied without needing 121 real requests.
        import time

        bucket_key = f"{ratelimit.KEY_PREFIX}{key_id}"
        await redis_client.hset(bucket_key, mapping={"tokens": "0", "ts": str(time.time())})

        resp = await client.post(
            f"/ingest/{project_id}/",
            headers={"X-Crashlens-Key": public_key},
            content=json.dumps(_valid_envelope()),
        )
        assert resp.status_code == 429
        assert "retry-after" in {k.lower() for k in resp.headers}
        assert int(resp.headers["retry-after"]) >= 1
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})
