"""Integration tests for the instance-admin panel (marked ``db``).

Like the sibling integration suites these need a live PostgreSQL with the
migrations applied and SKIP cleanly when none is reachable, so ``pytest -q``
passes locally without Postgres. In CI the postgres:16 service is up, the
migrations are applied, and these run for real.

Proves:

* every admin endpoint is instance-admin only (a normal user gets a uniform
  403; an unauthenticated caller gets 401);
* the first account created on a FRESH instance becomes the instance admin, and
  the next account does not;
* the instance-admin toggle works and the last-admin guard returns 400 when the
  sole admin tries to remove their own flag;
* ``admin_session`` reads counts across every org but CANNOT write and does not
  leak its BYPASSRLS to a plain session (an ``isolation``-marked test);
* the overview counts are correct across two orgs.

Several tests simulate a fresh instance by clearing the shared tables first;
each cleans up after itself and the suite runs serially, so this does not
disturb the other integration modules (which seed their own function-scoped
rows).
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import admin, security
from app.config import get_settings
from app.db import admin_session
from app.main import create_app

pytestmark = pytest.mark.db

_TEST_ROLE = "crashlens_test"
_TEST_PASSWORD = "crashlens_test"
_KNOWN_PASSWORD = "a-strong-test-passphrase"


@pytest_asyncio.fixture(scope="module")
async def superuser_engine():
    """Engine on the migration/superuser DATABASE_URL. Skips if unreachable."""
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("PostgreSQL not reachable; skipping admin integration tests")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="module")
async def app_sessionmaker(superuser_engine):
    """Session factory bound to a non-superuser role so RLS actually applies.

    The role is a member of all three privilege bundles, including
    ``crashlens_admin`` (migration 0007), which authorizes ``SET LOCAL ROLE
    crashlens_admin`` inside ``admin_session``.
    """
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crashlens_test') "
                "THEN CREATE ROLE crashlens_test LOGIN PASSWORD 'crashlens_test' "
                "NOSUPERUSER; END IF; "
                "END $$;"
            )
        )
        await conn.execute(text("GRANT crashlens_app TO crashlens_test"))
        await conn.execute(text("GRANT crashlens_system TO crashlens_test"))
        await conn.execute(text("GRANT crashlens_admin TO crashlens_test"))

    url = make_url(get_settings().database_url).set(
        username=_TEST_ROLE, password=_TEST_PASSWORD
    )
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _fresh_instance(conn) -> None:
    """Clear the instance so a 'fresh install' can be simulated deterministically.

    events carries no FK to orgs, so it is cleared explicitly; deleting orgs then
    users cascades everything else away.
    """
    await conn.execute(text("DELETE FROM events"))
    await conn.execute(text("DELETE FROM orgs"))
    await conn.execute(text("DELETE FROM users"))


async def _seed_user(conn, email: str, *, instance_admin: bool = False) -> uuid.UUID:
    user_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO users (id, email, password_hash, is_instance_admin) "
            "VALUES (:id, :email, :ph, :admin)"
        ),
        {
            "id": user_id,
            "email": email,
            "ph": security.hash_password(_KNOWN_PASSWORD),
            "admin": instance_admin,
        },
    )
    return user_id


async def _seed_org(conn, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO orgs (id, name, slug) VALUES (:id, :name, :slug)"),
        {"id": org_id, "name": name, "slug": f"{name}-{org_id}"},
    )
    return org_id


def _headers(user_id: uuid.UUID) -> dict:
    return {"Authorization": f"Bearer {security.create_access_token(user_id)}"}


# --- Every admin endpoint is instance-admin only ------------------------------
async def test_admin_endpoints_require_instance_admin(superuser_engine, client) -> None:
    async with superuser_engine.begin() as conn:
        plain_id = await _seed_user(
            conn, f"plain-{uuid.uuid4()}@example.test", instance_admin=False
        )
        target_id = await _seed_user(conn, f"target-{uuid.uuid4()}@example.test")
    plain_h = _headers(plain_id)
    try:
        toggle_path = f"/admin/users/{target_id}/instance-admin"
        # A normal (non-instance-admin) user is forbidden everywhere.
        for method, path in [
            ("GET", "/admin/overview"),
            ("GET", "/admin/orgs"),
            ("GET", "/admin/users"),
        ]:
            r = await client.request(method, path, headers=plain_h)
            assert r.status_code == 403, f"{method} {path} -> {r.status_code}"
            assert r.json()["detail"] == "Instance administrator access is required."
        toggle = await client.post(toggle_path, json={"enabled": True}, headers=plain_h)
        assert toggle.status_code == 403

        # Unauthenticated is 401, not 403 (no identity at all).
        assert (await client.get("/admin/overview")).status_code == 401
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM users WHERE id IN (:a, :b)"),
                {"a": plain_id, "b": target_id},
            )


async def test_instance_admin_can_read_overview(superuser_engine, client) -> None:
    async with superuser_engine.begin() as conn:
        admin_id = await _seed_user(
            conn, f"iadmin-{uuid.uuid4()}@example.test", instance_admin=True
        )
    try:
        r = await client.get("/admin/overview", headers=_headers(admin_id))
        assert r.status_code == 200
        body = r.json()
        for key in (
            "users_count",
            "orgs_count",
            "projects_count",
            "issues_count",
            "events_last_24h",
            "queue_depth",
            "partitions",
            "db_ok",
            "redis_ok",
        ):
            assert key in body
        assert body["db_ok"] is True
        assert isinstance(body["partitions"], list)
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": admin_id})


# --- First account on a fresh instance becomes the instance admin -------------
async def test_first_signup_on_fresh_instance_becomes_instance_admin(
    superuser_engine, app_sessionmaker
) -> None:
    from app import accounts

    async with superuser_engine.begin() as conn:
        await _fresh_instance(conn)
    first_email = f"first-{uuid.uuid4()}@example.test"
    second_email = f"second-{uuid.uuid4()}@example.test"
    try:
        first = await accounts.signup(
            first_email, _KNOWN_PASSWORD, "First Co", session_factory=app_sessionmaker
        )
        assert first is not None
        assert first["is_instance_admin"] is True

        second = await accounts.signup(
            second_email, _KNOWN_PASSWORD, "Second Co", session_factory=app_sessionmaker
        )
        assert second is not None
        assert second["is_instance_admin"] is False

        # Confirm it persisted, not just the return value.
        async with superuser_engine.connect() as conn:
            rows = dict(
                (
                    await conn.execute(
                        text(
                            "SELECT email, is_instance_admin FROM users "
                            "WHERE email IN (:a, :b)"
                        ),
                        {"a": first_email, "b": second_email},
                    )
                ).all()
            )
        assert rows[first_email] is True
        assert rows[second_email] is False
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM users WHERE email IN (:a, :b)"),
                {"a": first_email, "b": second_email},
            )
            await conn.execute(
                text("DELETE FROM orgs WHERE name IN ('First Co', 'Second Co')")
            )


# --- Toggle + last-admin guard ------------------------------------------------
async def test_toggle_and_last_admin_guard(superuser_engine, client) -> None:
    async with superuser_engine.begin() as conn:
        await _fresh_instance(conn)
        admin_a = await _seed_user(
            conn, f"a-{uuid.uuid4()}@example.test", instance_admin=True
        )
    admin_a_h = _headers(admin_a)
    admin_b = None
    try:
        # Sole admin removing their own flag would orphan the instance -> 400.
        self_off = await client.post(
            f"/admin/users/{admin_a}/instance-admin",
            json={"enabled": False},
            headers=admin_a_h,
        )
        assert self_off.status_code == 400

        # Grant a normal member the flag: allowed, and now there are two admins.
        async with superuser_engine.begin() as conn:
            admin_b = await _seed_user(conn, f"b-{uuid.uuid4()}@example.test")
        grant = await client.post(
            f"/admin/users/{admin_b}/instance-admin",
            json={"enabled": True},
            headers=admin_a_h,
        )
        assert grant.status_code == 200
        assert grant.json()["is_instance_admin"] is True

        # With a second admin present, A may now remove their own flag.
        self_off_again = await client.post(
            f"/admin/users/{admin_a}/instance-admin",
            json={"enabled": False},
            headers=admin_a_h,
        )
        assert self_off_again.status_code == 200
        assert self_off_again.json()["is_instance_admin"] is False

        # Unknown user id -> 404 (use a still-authorized admin token, B).
        missing = await client.post(
            f"/admin/users/{uuid.uuid4()}/instance-admin",
            json={"enabled": True},
            headers=_headers(admin_b),
        )
        assert missing.status_code == 404
    finally:
        async with superuser_engine.begin() as conn:
            ids = [i for i in (admin_a, admin_b) if i is not None]
            await conn.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": ids}
            )


# --- admin_session: cross-org read, no write, no leak to plain sessions --------
@pytest.mark.isolation
async def test_admin_session_reads_cross_org_but_cannot_write_or_leak(
    superuser_engine, app_sessionmaker
) -> None:
    async with superuser_engine.begin() as conn:
        org_a = await _seed_org(conn, "AdminIsoA")
        org_b = await _seed_org(conn, "AdminIsoB")
        proj_a = uuid.uuid4()
        proj_b = uuid.uuid4()
        for proj, org, slug in ((proj_a, org_a, "pa"), (proj_b, org_b, "pb")):
            await conn.execute(
                text(
                    "INSERT INTO projects (id, org_id, name, slug) "
                    "VALUES (:id, :org, :name, :slug)"
                ),
                {"id": proj, "org": org, "name": slug, "slug": slug},
            )
        for org, proj in ((org_a, proj_a), (org_b, proj_b)):
            await conn.execute(
                text(
                    "INSERT INTO issues (org_id, project_id, fingerprint, title, level) "
                    "VALUES (:org, :proj, :fp, :title, 'error')"
                ),
                {"org": org, "proj": proj, "fp": str(uuid.uuid4()), "title": "boom"},
            )
    try:
        # (1) admin_session reads BOTH orgs' issues in one query (BYPASSRLS).
        async with admin_session(session_factory=app_sessionmaker) as session:
            seen = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM issues WHERE org_id IN (:a, :b)"
                    ),
                    {"a": org_a, "b": org_b},
                )
            ).scalar_one()
        assert seen == 2

        # (2) admin_session is SELECT-only: a write raises a privilege error.
        with pytest.raises(DBAPIError):
            async with admin_session(session_factory=app_sessionmaker) as session:
                await session.execute(
                    text(
                        "INSERT INTO issues (org_id, project_id, fingerprint, title, level) "
                        "VALUES (:org, :proj, :fp, 'nope', 'error')"
                    ),
                    {"org": org_a, "proj": proj_a, "fp": str(uuid.uuid4())},
                )

        # (3) The BYPASSRLS does not leak: a plain session (login role, no
        # SET ROLE, no app.current_org GUC) is fully RLS-bound and sees nothing.
        async with app_sessionmaker() as session:
            async with session.begin():
                plain_seen = (
                    await session.execute(
                        text("SELECT count(*) FROM issues WHERE org_id IN (:a, :b)"),
                        {"a": org_a, "b": org_b},
                    )
                ).scalar_one()
        assert plain_seen == 0
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM orgs WHERE id IN (:a, :b)"), {"a": org_a, "b": org_b}
            )


# --- Overview counts are correct across two orgs ------------------------------
async def test_overview_counts_across_two_orgs(superuser_engine, app_sessionmaker) -> None:
    settings = get_settings()
    async with superuser_engine.begin() as conn:
        await _fresh_instance(conn)
        u1 = await _seed_user(conn, f"o1-{uuid.uuid4()}@example.test")
        u2 = await _seed_user(conn, f"o2-{uuid.uuid4()}@example.test")
        org_a = await _seed_org(conn, "OverviewA")
        org_b = await _seed_org(conn, "OverviewB")
        proj_a = uuid.uuid4()
        proj_b = uuid.uuid4()
        for proj, org, slug in ((proj_a, org_a, "pa"), (proj_b, org_b, "pb")):
            await conn.execute(
                text(
                    "INSERT INTO projects (id, org_id, name, slug) "
                    "VALUES (:id, :org, :name, :slug)"
                ),
                {"id": proj, "org": org, "name": slug, "slug": slug},
            )
        # One issue in org A only.
        await conn.execute(
            text(
                "INSERT INTO issues (org_id, project_id, fingerprint, title, level) "
                "VALUES (:org, :proj, :fp, 'boom', 'error')"
            ),
            {"org": org_a, "proj": proj_a, "fp": str(uuid.uuid4())},
        )
        # One event today in org B (received_at now -> today's partition exists).
        await conn.execute(
            text(
                "INSERT INTO events "
                "(org_id, project_id, event_id, received_at, environment, level, payload) "
                "VALUES (:org, :proj, :eid, now(), 'production', 'error', '{}'::jsonb)"
            ),
            {"org": org_b, "proj": proj_b, "eid": uuid.uuid4()},
        )
    try:
        data = await admin.get_overview(
            settings.database_url,
            settings.redis_url,
            session_factory=app_sessionmaker,
        )
        assert data["users_count"] == 2
        assert data["orgs_count"] == 2
        assert data["projects_count"] == 2
        assert data["issues_count"] == 1
        assert data["events_last_24h"] == 1
        assert data["db_ok"] is True
        # queue_depth is an int when Redis is up, else None; never negative.
        assert data["queue_depth"] is None or data["queue_depth"] >= 0
        # Partitions are the events_YYYYMMDD children, with non-negative estimates.
        assert all(p["name"].startswith("events_") for p in data["partitions"])
        assert all(p["row_estimate"] >= 0 for p in data["partitions"])
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM events"))
            await conn.execute(
                text("DELETE FROM orgs WHERE id IN (:a, :b)"), {"a": org_a, "b": org_b}
            )
            await conn.execute(
                text("DELETE FROM users WHERE id IN (:a, :b)"), {"a": u1, "b": u2}
            )
