"""Integration tests for the projects slice (marked ``db``).

Like ``test_auth_integration.py`` these require a live PostgreSQL with the
migrations applied and SKIP cleanly when none is reachable, so ``pytest -q``
passes locally without Postgres. In CI the postgres:16 service is up, the
migrations are applied, and these run for real.

Two kinds of tests:

* Service-level, run through a NON-superuser role (``crashlens_test``, member of
  both privilege bundles) so the real Row Level Security policies are in force.
  These prove the full project/DSN-key CRUD lifecycle, that a revoked key drops
  out of the active list, member listing, and cross-org isolation (org B sees
  nothing of org A and cannot act on it).
* HTTP-level, run through the ASGI app, proving the member-vs-admin-vs-outsider
  authZ matrix (403s) on every endpoint. These exercise authZ, not RLS.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import projects, security
from app.config import get_settings
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
        pytest.skip("PostgreSQL not reachable; skipping projects integration tests")
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

    url = make_url(get_settings().database_url).set(
        username=_TEST_ROLE, password=_TEST_PASSWORD
    )
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_user(conn, email: str, password: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO users (id, email, password_hash) VALUES (:id, :email, :ph)"),
        {"id": user_id, "email": email, "ph": security.hash_password(password)},
    )
    return user_id


async def _seed_org(conn, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO orgs (id, name, slug) VALUES (:id, :name, :slug)"),
        {"id": org_id, "name": name, "slug": f"{name}-{org_id}"},
    )
    return org_id


async def _add_membership(conn, org_id, user_id, role) -> None:
    await conn.execute(
        text(
            "INSERT INTO org_memberships (org_id, user_id, role) "
            "VALUES (:oid, :uid, :role)"
        ),
        {"oid": org_id, "uid": user_id, "role": role},
    )


# --- Service-level under real RLS ---------------------------------------------
async def test_project_and_key_crud_lifecycle(
    app_sessionmaker, superuser_engine
) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "CrudCo")
    try:
        # Create + list.
        created = await projects.create_project(
            org_id, "Payments API", "python", session_factory=app_sessionmaker
        )
        assert created is not None
        assert created["name"] == "Payments API"
        assert created["platform"] == "python"
        assert created["slug"].startswith("payments-api-")

        listed = await projects.list_projects(org_id, session_factory=app_sessionmaker)
        assert [p["id"] for p in listed] == [created["id"]]

        # Detail with no keys yet.
        detail = await projects.get_project(
            org_id, created["id"], session_factory=app_sessionmaker
        )
        assert detail is not None
        assert detail["keys"] == []

        # Create a key; it appears active in the detail.
        key = await projects.create_dsn_key(
            org_id, created["id"], session_factory=app_sessionmaker
        )
        assert key is not None
        assert key["status"] == "active"
        assert key["public_key"]

        detail = await projects.get_project(
            org_id, created["id"], session_factory=app_sessionmaker
        )
        assert detail is not None
        assert [k["id"] for k in detail["keys"]] == [key["id"]]

        # Revoke it; it drops out of the active detail.
        revoked = await projects.revoke_dsn_key(
            org_id, created["id"], key["id"], session_factory=app_sessionmaker
        )
        assert revoked is True
        detail = await projects.get_project(
            org_id, created["id"], session_factory=app_sessionmaker
        )
        assert detail is not None
        assert detail["keys"] == []

        # Re-revoking the same key is a no-op (already revoked).
        assert (
            await projects.revoke_dsn_key(
                org_id, created["id"], key["id"], session_factory=app_sessionmaker
            )
            is False
        )

        # Delete the project.
        assert (
            await projects.delete_project(
                org_id, created["id"], session_factory=app_sessionmaker
            )
            is True
        )
        assert (
            await projects.list_projects(org_id, session_factory=app_sessionmaker) == []
        )
        assert (
            await projects.delete_project(
                org_id, created["id"], session_factory=app_sessionmaker
            )
            is False
        )
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})


async def test_revoked_key_excluded_from_active_list(
    app_sessionmaker, superuser_engine
) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "KeysCo")
    try:
        project = await projects.create_project(
            org_id, "Web", None, session_factory=app_sessionmaker
        )
        assert project is not None
        key_a = await projects.create_dsn_key(
            org_id, project["id"], session_factory=app_sessionmaker
        )
        key_b = await projects.create_dsn_key(
            org_id, project["id"], session_factory=app_sessionmaker
        )
        assert key_a is not None and key_b is not None

        await projects.revoke_dsn_key(
            org_id, project["id"], key_a["id"], session_factory=app_sessionmaker
        )
        detail = await projects.get_project(
            org_id, project["id"], session_factory=app_sessionmaker
        )
        assert detail is not None
        active_ids = {k["id"] for k in detail["keys"]}
        assert active_ids == {key_b["id"]}
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})


async def test_list_members_returns_emails_and_roles(
    app_sessionmaker, superuser_engine
) -> None:
    admin_email = f"admin-{uuid.uuid4()}@example.test"
    member_email = f"member-{uuid.uuid4()}@example.test"
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "MembersCo")
        admin_id = await _seed_user(conn, admin_email, _KNOWN_PASSWORD)
        member_id = await _seed_user(conn, member_email, _KNOWN_PASSWORD)
        await _add_membership(conn, org_id, admin_id, "admin")
        await _add_membership(conn, org_id, member_id, "member")
    try:
        members = await projects.list_members(
            org_id, session_factory=app_sessionmaker
        )
        by_email = {m["email"]: m["role"] for m in members}
        assert by_email == {admin_email: "admin", member_email: "member"}
        assert all(m["user_id"] is not None for m in members)
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})
            await conn.execute(
                text("DELETE FROM users WHERE id IN (:a, :m)"),
                {"a": admin_id, "m": member_id},
            )


async def test_cross_org_isolation_under_rls(
    app_sessionmaker, superuser_engine
) -> None:
    async with superuser_engine.begin() as conn:
        org_a = await _seed_org(conn, "OrgA")
        org_b = await _seed_org(conn, "OrgB")
    try:
        project = await projects.create_project(
            org_a, "Secret A", None, session_factory=app_sessionmaker
        )
        assert project is not None
        key = await projects.create_dsn_key(
            org_a, project["id"], session_factory=app_sessionmaker
        )
        assert key is not None

        # Org B sees nothing of org A and cannot act on its project or key.
        assert (
            await projects.list_projects(org_b, session_factory=app_sessionmaker) == []
        )
        assert (
            await projects.get_project(
                org_b, project["id"], session_factory=app_sessionmaker
            )
            is None
        )
        assert (
            await projects.create_dsn_key(
                org_b, project["id"], session_factory=app_sessionmaker
            )
            is None
        )
        assert (
            await projects.revoke_dsn_key(
                org_b, project["id"], key["id"], session_factory=app_sessionmaker
            )
            is False
        )
        assert (
            await projects.delete_project(
                org_b, project["id"], session_factory=app_sessionmaker
            )
            is False
        )

        # Org A still sees its own project intact (org B's failed calls changed
        # nothing).
        detail = await projects.get_project(
            org_a, project["id"], session_factory=app_sessionmaker
        )
        assert detail is not None
        assert [k["id"] for k in detail["keys"]] == [key["id"]]
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM orgs WHERE id IN (:a, :b)"),
                {"a": org_a, "b": org_b},
            )


# --- HTTP-level authZ matrix --------------------------------------------------
@pytest_asyncio.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_project_endpoints_enforce_member_and_admin_authz(
    superuser_engine, client
) -> None:
    admin_email = f"padmin-{uuid.uuid4()}@example.test"
    member_email = f"pmember-{uuid.uuid4()}@example.test"
    outsider_email = f"poutsider-{uuid.uuid4()}@example.test"
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "AuthzProjCo")
        admin_id = await _seed_user(conn, admin_email, _KNOWN_PASSWORD)
        member_id = await _seed_user(conn, member_email, _KNOWN_PASSWORD)
        outsider_id = await _seed_user(conn, outsider_email, _KNOWN_PASSWORD)
        await _add_membership(conn, org_id, admin_id, "admin")
        await _add_membership(conn, org_id, member_id, "member")
    admin_h = {"Authorization": f"Bearer {security.create_access_token(admin_id)}"}
    member_h = {"Authorization": f"Bearer {security.create_access_token(member_id)}"}
    outsider_h = {
        "Authorization": f"Bearer {security.create_access_token(outsider_id)}"
    }
    created_project_id: str | None = None
    try:
        base = f"/orgs/{org_id}/projects"

        # A member may read but not create; an outsider is refused outright.
        assert (await client.get(base, headers=member_h)).status_code == 200
        assert (await client.get(base, headers=outsider_h)).status_code == 403
        assert (
            await client.post(base, json={"name": "X"}, headers=member_h)
        ).status_code == 403

        # An admin creates a project.
        r_create = await client.post(
            base, json={"name": "Ledger", "platform": "node"}, headers=admin_h
        )
        assert r_create.status_code == 201
        created_project_id = r_create.json()["id"]
        detail_url = f"{base}/{created_project_id}"
        keys_url = f"{detail_url}/keys"

        # Member reads detail + members; cannot mint a key or delete.
        assert (await client.get(detail_url, headers=member_h)).status_code == 200
        assert (
            await client.get(f"/orgs/{org_id}/members", headers=member_h)
        ).status_code == 200
        assert (await client.post(keys_url, headers=member_h)).status_code == 403
        assert (await client.delete(detail_url, headers=member_h)).status_code == 403

        # Admin mints and revokes a key.
        r_key = await client.post(keys_url, headers=admin_h)
        assert r_key.status_code == 201
        key_id = r_key.json()["id"]
        assert r_key.json()["public_key"]
        r_revoke = await client.post(
            f"{keys_url}/{key_id}/revoke", headers=admin_h
        )
        assert r_revoke.status_code == 204

        # Outsider is refused on the detail read too.
        assert (await client.get(detail_url, headers=outsider_h)).status_code == 403

        # Admin deletes the project.
        assert (
            await client.delete(detail_url, headers=admin_h)
        ).status_code == 204
        created_project_id = None
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})
            await conn.execute(
                text("DELETE FROM users WHERE id IN (:a, :m, :o)"),
                {"a": admin_id, "m": member_id, "o": outsider_id},
            )
