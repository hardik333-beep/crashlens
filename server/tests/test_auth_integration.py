"""Integration tests for the auth slice (marked ``db``).

Like ``test_db_integration.py`` these require a live PostgreSQL with both
migrations applied and SKIP cleanly when none is reachable, so ``pytest -q``
passes locally without Postgres. In CI the postgres:16 service is up, the
migrations are applied, and these run for real.

Two kinds of tests here:

* Service-level, exercised through a NON-superuser role (``crashlens_test``,
  member of both privilege bundles) so the real Row Level Security policies are
  in force. These prove signup atomicity, the lockout state machine, and that
  invites store only a hash and grant membership under RLS.
* HTTP-level, exercised through the ASGI app (whose default session uses the
  configured DATABASE_URL). These prove the identical-401 login contract, that
  /auth/me needs a valid token, and that the invite endpoint rejects non-admins
  with 403. They do not depend on RLS enforcement, only on the authZ logic.
"""

import datetime
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import accounts, security
from app.main import create_app
from tests.conftest import superuser_database_url

pytestmark = pytest.mark.db

_TEST_ROLE = "crashlens_test"
_TEST_PASSWORD = "crashlens_test"
_KNOWN_PASSWORD = "a-strong-test-passphrase"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


@pytest_asyncio.fixture(scope="module")
async def superuser_engine():
    """Engine on the migration/superuser DATABASE_URL. Skips if unreachable."""
    engine = create_async_engine(superuser_database_url())
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("PostgreSQL not reachable; skipping auth integration tests")
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


async def _seed_user(conn, email: str, password: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO users (id, email, password_hash) VALUES (:id, :email, :ph)"
        ),
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
async def test_signup_creates_user_org_and_admin_membership_atomically(
    app_sessionmaker, superuser_engine
) -> None:
    email = f"founder-{uuid.uuid4()}@example.test"
    created = await accounts.signup(
        email, _KNOWN_PASSWORD, "Acme Inc", session_factory=app_sessionmaker
    )
    assert created is not None
    try:
        async with superuser_engine.connect() as conn:
            user_row = (
                await conn.execute(
                    text("SELECT id FROM users WHERE email = :email"), {"email": email}
                )
            ).scalar_one()
            assert user_row == created["user_id"]
            org_row = (
                await conn.execute(
                    text("SELECT name FROM orgs WHERE id = :id"),
                    {"id": created["org_id"]},
                )
            ).scalar_one()
            assert org_row == "Acme Inc"
            role = (
                await conn.execute(
                    text(
                        "SELECT role FROM org_memberships "
                        "WHERE org_id = :oid AND user_id = :uid"
                    ),
                    {"oid": created["org_id"], "uid": created["user_id"]},
                )
            ).scalar_one()
            assert role == "admin"
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM orgs WHERE id = :id"), {"id": created["org_id"]}
            )
            await conn.execute(
                text("DELETE FROM users WHERE id = :id"), {"id": created["user_id"]}
            )


async def test_signup_with_existing_email_creates_nothing(
    app_sessionmaker, superuser_engine
) -> None:
    email = f"dup-{uuid.uuid4()}@example.test"
    async with superuser_engine.begin() as conn:
        existing_id = await _seed_user(conn, email, _KNOWN_PASSWORD)
    try:
        result = await accounts.signup(
            email, _KNOWN_PASSWORD, "Second Org", session_factory=app_sessionmaker
        )
        assert result is None
        async with superuser_engine.connect() as conn:
            org_count = (
                await conn.execute(
                    text("SELECT count(*) FROM orgs WHERE name = :name"),
                    {"name": "Second Org"},
                )
            ).scalar_one()
            assert org_count == 0
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM users WHERE id = :id"), {"id": existing_id}
            )


async def test_login_locks_after_ten_failures_and_unlocks_after_window(
    app_sessionmaker, superuser_engine
) -> None:
    email = f"lockme-{uuid.uuid4()}@example.test"
    async with superuser_engine.begin() as conn:
        user_id = await _seed_user(conn, email, _KNOWN_PASSWORD)
    try:
        # Ten consecutive wrong-password attempts.
        for _ in range(security.LOCKOUT_THRESHOLD):
            assert (
                await accounts.authenticate(
                    email, "wrong-password", session_factory=app_sessionmaker
                )
                is None
            )

        async with superuser_engine.connect() as conn:
            locked_until = (
                await conn.execute(
                    text("SELECT locked_until FROM users WHERE id = :id"),
                    {"id": user_id},
                )
            ).scalar_one()
        assert locked_until is not None and locked_until > _utcnow()

        # Even the CORRECT password is refused while locked.
        assert (
            await accounts.authenticate(
                email, _KNOWN_PASSWORD, session_factory=app_sessionmaker
            )
            is None
        )

        # Simulate the window elapsing.
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET locked_until = :past WHERE id = :id"),
                {"past": _utcnow() - datetime.timedelta(minutes=1), "id": user_id},
            )

        # Correct password now succeeds and resets the counter and lock.
        user = await accounts.authenticate(
            email, _KNOWN_PASSWORD, session_factory=app_sessionmaker
        )
        assert user is not None
        async with superuser_engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT failed_login_count, locked_until, last_login_at "
                        "FROM users WHERE id = :id"
                    ),
                    {"id": user_id},
                )
            ).one()
        assert row.failed_login_count == 0
        assert row.locked_until is None
        assert row.last_login_at is not None
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


async def test_authenticate_returns_none_uniformly_for_all_failure_modes(
    app_sessionmaker, superuser_engine
) -> None:
    email = f"uniform-{uuid.uuid4()}@example.test"
    async with superuser_engine.begin() as conn:
        user_id = await _seed_user(conn, email, _KNOWN_PASSWORD)
    try:
        unknown = await accounts.authenticate(
            f"nobody-{uuid.uuid4()}@example.test",
            _KNOWN_PASSWORD,
            session_factory=app_sessionmaker,
        )
        wrong = await accounts.authenticate(
            email, "wrong-password", session_factory=app_sessionmaker
        )
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET locked_until = :future WHERE id = :id"),
                {"future": _utcnow() + datetime.timedelta(minutes=15), "id": user_id},
            )
        locked = await accounts.authenticate(
            email, _KNOWN_PASSWORD, session_factory=app_sessionmaker
        )
        assert unknown is None
        assert wrong is None
        assert locked is None
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


async def test_create_invite_stores_only_the_hash(
    app_sessionmaker, superuser_engine
) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "InviteCo")
    try:
        invite, raw_token = await accounts.create_invite(
            org_id, "invitee@example.test", "member", session_factory=app_sessionmaker,
            actor_user_id=None,
        )
        async with superuser_engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT token_hash, email, role FROM org_invites "
                        "WHERE id = :id"
                    ),
                    {"id": invite["id"]},
                )
            ).one()
        assert row.token_hash == security.hash_invite_token(raw_token)
        # The raw secret is never persisted anywhere on the row.
        assert row.token_hash != raw_token
        assert row.email == "invitee@example.test"
        assert row.role == "member"
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})


async def test_accept_invite_grants_membership(
    app_sessionmaker, superuser_engine
) -> None:
    email = f"newmember-{uuid.uuid4()}@example.test"
    raw_token = security.generate_invite_token()
    invite_id = uuid.uuid4()
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "AcceptCo")
        await conn.execute(
            text(
                "INSERT INTO org_invites "
                "(id, org_id, email, role, token_hash, expires_at) "
                "VALUES (:id, :oid, :email, 'member', :th, :exp)"
            ),
            {
                "id": invite_id,
                "oid": org_id,
                "email": email,
                "th": security.hash_invite_token(raw_token),
                "exp": _utcnow() + datetime.timedelta(days=1),
            },
        )
    result = None
    try:
        result = await accounts.accept_invite(
            raw_token, email, _KNOWN_PASSWORD, session_factory=app_sessionmaker
        )
        assert result is not None
        async with superuser_engine.connect() as conn:
            role = (
                await conn.execute(
                    text(
                        "SELECT role FROM org_memberships "
                        "WHERE org_id = :oid AND user_id = :uid"
                    ),
                    {"oid": org_id, "uid": result["user_id"]},
                )
            ).scalar_one()
            assert role == "member"
            accepted_at = (
                await conn.execute(
                    text("SELECT accepted_at FROM org_invites WHERE id = :id"),
                    {"id": invite_id},
                )
            ).scalar_one()
            assert accepted_at is not None
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})
            if result is not None:
                await conn.execute(
                    text("DELETE FROM users WHERE id = :id"),
                    {"id": result["user_id"]},
                )


# --- HTTP-level authZ contracts -----------------------------------------------
@pytest_asyncio.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_login_returns_identical_401_for_unknown_wrong_and_locked(
    superuser_engine, client
) -> None:
    email = f"login401-{uuid.uuid4()}@example.test"
    async with superuser_engine.begin() as conn:
        user_id = await _seed_user(conn, email, _KNOWN_PASSWORD)
    try:
        r_unknown = await client.post(
            "/auth/login",
            json={"email": f"ghost-{uuid.uuid4()}@example.test", "password": "whatever10"},
        )
        r_wrong = await client.post(
            "/auth/login", json={"email": email, "password": "wrong-password"}
        )
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET locked_until = :future WHERE id = :id"),
                {"future": _utcnow() + datetime.timedelta(minutes=15), "id": user_id},
            )
        r_locked = await client.post(
            "/auth/login", json={"email": email, "password": _KNOWN_PASSWORD}
        )
        assert r_unknown.status_code == r_wrong.status_code == r_locked.status_code == 401
        assert r_unknown.json() == r_wrong.json() == r_locked.json()
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


async def test_me_requires_valid_token_and_returns_user(
    superuser_engine, client
) -> None:
    email = f"meuser-{uuid.uuid4()}@example.test"
    async with superuser_engine.begin() as conn:
        user_id = await _seed_user(conn, email, _KNOWN_PASSWORD)
    try:
        token = security.create_access_token(user_id)
        response = await client.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.json()["user"]["id"] == str(user_id)
        assert response.json()["user"]["email"] == email
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


@pytest.mark.isolation
async def test_org_invites_rejects_non_member_and_non_admin_with_403(
    superuser_engine, client
) -> None:
    admin_email = f"admin-{uuid.uuid4()}@example.test"
    member_email = f"member-{uuid.uuid4()}@example.test"
    outsider_email = f"outsider-{uuid.uuid4()}@example.test"
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "AuthzCo")
        admin_id = await _seed_user(conn, admin_email, _KNOWN_PASSWORD)
        member_id = await _seed_user(conn, member_email, _KNOWN_PASSWORD)
        outsider_id = await _seed_user(conn, outsider_email, _KNOWN_PASSWORD)
        await _add_membership(conn, org_id, admin_id, "admin")
        await _add_membership(conn, org_id, member_id, "member")
    try:
        admin_token = security.create_access_token(admin_id)
        member_token = security.create_access_token(member_id)
        outsider_token = security.create_access_token(outsider_id)
        payload = {"email": "someone@example.test", "role": "member"}

        r_outsider = await client.post(
            f"/orgs/{org_id}/invites",
            json=payload,
            headers={"Authorization": f"Bearer {outsider_token}"},
        )
        r_member = await client.post(
            f"/orgs/{org_id}/invites",
            json=payload,
            headers={"Authorization": f"Bearer {member_token}"},
        )
        r_admin = await client.post(
            f"/orgs/{org_id}/invites",
            json=payload,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_outsider.status_code == 403
        assert r_member.status_code == 403
        assert r_admin.status_code == 201
        assert r_admin.json()["token"]  # raw token returned once
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})
            await conn.execute(
                text("DELETE FROM users WHERE id IN (:a, :m, :o)"),
                {"a": admin_id, "m": member_id, "o": outsider_id},
            )
