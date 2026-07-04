"""End-to-end cross-tenant isolation gate (marked ``db`` and ``isolation``).

This is the hard CI gate for W1-04: it tells the complete cross-tenant story at
the HTTP level (the surface an attacker actually has) AND at the direct-DB
level (the RLS enforcement underneath), in one test. Like the other
``db``-marked integration tests it requires a live PostgreSQL with the
migration applied and SKIPS cleanly when none is reachable, so ``pytest -q``
passes locally without Postgres; in CI the ``cross-tenant-isolation`` job
starts a postgres:16 service, runs ``alembic upgrade head``, then runs
``pytest -q -m isolation`` as a hard, non-continue-on-error gate.

Story: org A and org B are both created through the real public signup flow
(``POST /auth/signup``), each mints a project and a DSN key through the real
authenticated API. Org A's own admin token is then used to attack org B:

* listing org B's projects -> 403
* reading org B's project detail -> 403
* revoking org B's key -> 403

Then, independent of the HTTP authZ layer, a non-superuser DB role scoped to
org A via ``tenant_session`` is asked directly for org B's rows in ``projects``
and ``dsn_keys`` -> zero, proving the isolation is structural (RLS), not just
an application-layer check that a future endpoint could forget to add.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import tenant_session
from app.main import create_app
from tests.conftest import superuser_database_url

pytestmark = [pytest.mark.db, pytest.mark.isolation]

_TEST_ROLE = "crashlens_test"
_TEST_PASSWORD = "crashlens_test"
_SIGNUP_PASSWORD = "a-strong-test-passphrase"


@pytest_asyncio.fixture(scope="module")
async def superuser_engine():
    """Engine on the migration/superuser DATABASE_URL. Skips if unreachable."""
    engine = create_async_engine(superuser_database_url())
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("PostgreSQL not reachable; skipping cross-tenant gate test")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="module")
async def app_sessionmaker(superuser_engine):
    """Session factory bound to a non-superuser role so RLS actually applies."""
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

    url = make_url(superuser_database_url()).set(
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


async def _signup(client: AsyncClient, org_name: str) -> dict:
    response = await client.post(
        "/auth/signup",
        json={
            "email": f"{org_name.lower()}-{uuid.uuid4()}@example.test",
            "password": _SIGNUP_PASSWORD,
            "org_name": org_name,
        },
    )
    assert response.status_code == 201
    body = response.json()
    return {
        "token": body["token"],
        "user_id": uuid.UUID(body["user"]["id"]),
        "org_id": uuid.UUID(body["org"]["id"]),
    }


async def test_cross_tenant_gate_http_and_db(
    client: AsyncClient, app_sessionmaker, superuser_engine
) -> None:
    org_a = await _signup(client, f"GateOrgA-{uuid.uuid4()}")
    org_b = await _signup(client, f"GateOrgB-{uuid.uuid4()}")
    a_headers = {"Authorization": f"Bearer {org_a['token']}"}
    b_headers = {"Authorization": f"Bearer {org_b['token']}"}

    try:
        # Each org mints its own project and DSN key through the real,
        # authenticated API (its own admin token against its own org id).
        r_proj_a = await client.post(
            f"/orgs/{org_a['org_id']}/projects",
            json={"name": "A Service", "platform": "python"},
            headers=a_headers,
        )
        assert r_proj_a.status_code == 201

        r_proj_b = await client.post(
            f"/orgs/{org_b['org_id']}/projects",
            json={"name": "B Service", "platform": "node"},
            headers=b_headers,
        )
        assert r_proj_b.status_code == 201
        proj_b_id = r_proj_b.json()["id"]

        r_key_b = await client.post(
            f"/orgs/{org_b['org_id']}/projects/{proj_b_id}/keys", headers=b_headers
        )
        assert r_key_b.status_code == 201
        key_b_id = r_key_b.json()["id"]

        # --- HTTP-level attack: org A's own admin token against org B's path.
        # ``require_org_member``/``require_org_admin`` verify the PATH org id
        # against A's membership, so every one of these must be 403, never a
        # 404 (which would leak that B's resource exists) or a 200/204.
        r_list = await client.get(f"/orgs/{org_b['org_id']}/projects", headers=a_headers)
        assert r_list.status_code == 403

        r_detail = await client.get(
            f"/orgs/{org_b['org_id']}/projects/{proj_b_id}", headers=a_headers
        )
        assert r_detail.status_code == 403

        r_revoke = await client.post(
            f"/orgs/{org_b['org_id']}/projects/{proj_b_id}/keys/{key_b_id}/revoke",
            headers=a_headers,
        )
        assert r_revoke.status_code == 403

        # B's key must have survived A's revoke attempt untouched: confirmed
        # from B's own authenticated view.
        r_still_active = await client.get(
            f"/orgs/{org_b['org_id']}/projects/{proj_b_id}", headers=b_headers
        )
        assert r_still_active.status_code == 200
        assert [k["id"] for k in r_still_active.json()["keys"]] == [key_b_id]

        # --- DB-level: independent of the HTTP authZ layer, a non-superuser
        # session scoped to org A via RLS must see ZERO of org B's rows in
        # both projects and dsn_keys, even by direct id lookup.
        async with tenant_session(
            str(org_a["org_id"]), session_factory=app_sessionmaker
        ) as session:
            visible_projects = (
                await session.execute(
                    text("SELECT id FROM projects WHERE id = :pid"),
                    {"pid": proj_b_id},
                )
            ).scalars().all()
            assert visible_projects == []

            visible_keys = (
                await session.execute(
                    text("SELECT id FROM dsn_keys WHERE id = :kid"),
                    {"kid": key_b_id},
                )
            ).scalars().all()
            assert visible_keys == []

            total_projects_from_a = (
                await session.execute(text("SELECT count(*) FROM projects"))
            ).scalar_one()
            assert total_projects_from_a == 1

            total_keys_from_a = (
                await session.execute(text("SELECT count(*) FROM dsn_keys"))
            ).scalar_one()
            assert total_keys_from_a == 0  # A minted no key on its own project
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM orgs WHERE id IN (:a, :b)"),
                {"a": org_a["org_id"], "b": org_b["org_id"]},
            )
            await conn.execute(
                text("DELETE FROM users WHERE id IN (:a, :b)"),
                {"a": org_a["user_id"], "b": org_b["user_id"]},
            )
