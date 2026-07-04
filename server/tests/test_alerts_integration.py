"""Integration tests for the alerts slice (marked ``db``).

Like the sibling integration files these require a live PostgreSQL with the
migrations applied and SKIP cleanly when none is reachable, so ``pytest -q``
passes locally without Postgres. In CI the postgres service is up, the migrations
are applied, and these run for real.

Three kinds of tests:

* Service-level channel CRUD run through the NON-superuser ``crashlens_test``
  role so the real Row Level Security policies are in force, including cross-org
  isolation (an ``isolation``-marked test).
* HTTP-level authZ matrix (member-vs-admin-vs-outsider 403s) through the ASGI app.
* ``dispatch_alerts`` fan-out with the network senders MONKEYPATCHED to record
  per-channel calls: org-wide vs project-scoped selection, disabled channels
  skipped, and one failing channel not stopping the others.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import alerts, projects, security
from app.jobs import alerts as dispatch
from app.main import create_app
from tests.conftest import superuser_database_url

pytestmark = pytest.mark.db

_TEST_ROLE = "crashlens_test"
_TEST_PASSWORD = "crashlens_test"
_KNOWN_PASSWORD = "a-strong-test-passphrase"


@pytest_asyncio.fixture(scope="module")
async def superuser_engine():
    """Engine on the migration/superuser DATABASE_URL. Skips if unreachable."""
    engine = create_async_engine(superuser_database_url())
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("PostgreSQL not reachable; skipping alerts integration tests")
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


# --- Service-level channel CRUD under real RLS --------------------------------
async def test_channel_crud_lifecycle(app_sessionmaker, superuser_engine) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "AlertCo")
    try:
        # Create an org-wide slack channel.
        created = await alerts.create_channel(
            org_id,
            "slack",
            {"webhook_url": "https://hooks.slack.com/services/T/B/X"},
            None,
            session_factory=app_sessionmaker,
            actor_user_id=None,
        )
        assert created is not None
        assert created["type"] == "slack"
        assert created["project_id"] is None
        assert created["enabled"] is True

        listed = await alerts.list_channels(org_id, session_factory=app_sessionmaker)
        assert [c["id"] for c in listed] == [created["id"]]

        # Disable it.
        updated = await alerts.update_channel(
            org_id, created["id"], enabled=False, session_factory=app_sessionmaker,
            actor_user_id=None,
        )
        assert updated is not None
        assert updated["enabled"] is False

        # Replace its config (still a slack channel; type is not changeable).
        reconfigured = await alerts.update_channel(
            org_id,
            created["id"],
            config={"webhook_url": "https://hooks.slack.com/services/T/B/Y"},
            session_factory=app_sessionmaker,
            actor_user_id=None,
        )
        assert reconfigured is not None
        assert reconfigured["config"]["webhook_url"].endswith("/Y")

        # Delete it.
        assert (
            await alerts.delete_channel(
                org_id, created["id"], session_factory=app_sessionmaker,
                actor_user_id=None,
            )
            is True
        )
        assert (
            await alerts.list_channels(org_id, session_factory=app_sessionmaker) == []
        )
        # Deleting again is a no-op.
        assert (
            await alerts.delete_channel(
                org_id, created["id"], session_factory=app_sessionmaker,
                actor_user_id=None,
            )
            is False
        )
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})


async def test_project_scoped_channel_requires_project_in_org(
    app_sessionmaker, superuser_engine
) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "ScopeCo")
    try:
        project = await projects.create_project(
            org_id, "Web", None, session_factory=app_sessionmaker,
            actor_user_id=None,
        )
        assert project is not None

        scoped = await alerts.create_channel(
            org_id,
            "webhook",
            {"url": "https://ops.example.com/hook"},
            uuid.UUID(project["id"]) if isinstance(project["id"], str) else project["id"],
            session_factory=app_sessionmaker,
            actor_user_id=None,
        )
        assert scoped is not None
        assert str(scoped["project_id"]) == str(project["id"])

        # A random (non-existent) project id is rejected with None -> 404.
        missing = await alerts.create_channel(
            org_id,
            "webhook",
            {"url": "https://ops.example.com/hook"},
            uuid.uuid4(),
            session_factory=app_sessionmaker,
            actor_user_id=None,
        )
        assert missing is None
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})


@pytest.mark.isolation
async def test_cross_org_channel_isolation(app_sessionmaker, superuser_engine) -> None:
    async with superuser_engine.begin() as conn:
        org_a = await _seed_org(conn, "AlertOrgA")
        org_b = await _seed_org(conn, "AlertOrgB")
    try:
        channel = await alerts.create_channel(
            org_a,
            "slack",
            {"webhook_url": "https://hooks.slack.com/services/A/A/A"},
            None,
            session_factory=app_sessionmaker,
            actor_user_id=None,
        )
        assert channel is not None

        # Org B sees nothing of org A and cannot act on its channel.
        assert (
            await alerts.list_channels(org_b, session_factory=app_sessionmaker) == []
        )
        assert (
            await alerts.update_channel(
                org_b, channel["id"], enabled=False, session_factory=app_sessionmaker,
                actor_user_id=None,
            )
            is None
        )
        assert (
            await alerts.delete_channel(
                org_b, channel["id"], session_factory=app_sessionmaker,
                actor_user_id=None,
            )
            is False
        )

        # Org A still sees its channel intact.
        listed = await alerts.list_channels(org_a, session_factory=app_sessionmaker)
        assert [c["id"] for c in listed] == [channel["id"]]
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM orgs WHERE id IN (:a, :b)"), {"a": org_a, "b": org_b}
            )


# --- HTTP authZ matrix --------------------------------------------------------
@pytest_asyncio.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.isolation
async def test_alert_channel_endpoints_enforce_member_and_admin_authz(
    superuser_engine, client
) -> None:
    admin_email = f"aadmin-{uuid.uuid4()}@example.test"
    member_email = f"amember-{uuid.uuid4()}@example.test"
    outsider_email = f"aoutsider-{uuid.uuid4()}@example.test"
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "AuthzAlertCo")
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
    try:
        base = f"/orgs/{org_id}/alert-channels"
        new_channel = {
            "type": "slack",
            "config": {"webhook_url": "https://hooks.slack.com/services/T/B/X"},
        }

        # Member reads; outsider is refused; member cannot create.
        assert (await client.get(base, headers=member_h)).status_code == 200
        assert (await client.get(base, headers=outsider_h)).status_code == 403
        assert (
            await client.post(base, json=new_channel, headers=member_h)
        ).status_code == 403

        # Admin creates; the response never echoes the full secret URL.
        r_create = await client.post(base, json=new_channel, headers=admin_h)
        assert r_create.status_code == 201
        created = r_create.json()
        channel_id = created["id"]
        assert created["target"] == "https://hooks.slack.com/..."
        assert "X" not in created["target"]

        one = f"{base}/{channel_id}"

        # Member cannot toggle or delete.
        assert (
            await client.patch(one, json={"enabled": False}, headers=member_h)
        ).status_code == 403
        assert (await client.delete(one, headers=member_h)).status_code == 403

        # Admin toggles then deletes.
        r_patch = await client.patch(one, json={"enabled": False}, headers=admin_h)
        assert r_patch.status_code == 200
        assert r_patch.json()["enabled"] is False
        assert (await client.delete(one, headers=admin_h)).status_code == 204

        # Bad config is a 400.
        assert (
            await client.post(
                base,
                json={"type": "slack", "config": {"webhook_url": "http://insecure"}},
                headers=admin_h,
            )
        ).status_code == 400
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})
            await conn.execute(
                text("DELETE FROM users WHERE id IN (:a, :m, :o)"),
                {"a": admin_id, "m": member_id, "o": outsider_id},
            )


# --- dispatch_alerts fan-out with monkeypatched senders -----------------------
@pytest_asyncio.fixture
async def dispatch_org(superuser_engine, app_sessionmaker):
    """Seed one org with two projects. Cleaned up on teardown."""
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "DispatchCo")
    project_1 = await projects.create_project(
        org_id, "Project One", None, session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    project_2 = await projects.create_project(
        org_id, "Project Two", None, session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    assert project_1 is not None and project_2 is not None
    yield {"org_id": org_id, "project_1": project_1["id"], "project_2": project_2["id"]}
    async with superuser_engine.begin() as conn:
        await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})


async def test_dispatch_selects_org_wide_and_project_scoped_only(
    superuser_engine, app_sessionmaker, dispatch_org, monkeypatch
) -> None:
    org_id = dispatch_org["org_id"]
    project_1 = dispatch_org["project_1"]
    project_2 = dispatch_org["project_2"]

    def pid(value):
        return uuid.UUID(value) if isinstance(value, str) else value

    # org-wide slack, project-1 webhook, project-2 slack (must NOT fire for p1),
    # and a disabled org-wide slack (must be skipped).
    await alerts.create_channel(
        org_id, "slack", {"webhook_url": "https://h.example.com/orgwide"}, None,
        session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    await alerts.create_channel(
        org_id, "webhook", {"url": "https://h.example.com/proj1"}, pid(project_1),
        session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    await alerts.create_channel(
        org_id, "slack", {"webhook_url": "https://h.example.com/proj2"}, pid(project_2),
        session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    disabled = await alerts.create_channel(
        org_id, "slack", {"webhook_url": "https://h.example.com/disabled"}, None,
        session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    assert disabled is not None
    await alerts.update_channel(
        org_id, disabled["id"], enabled=False, session_factory=app_sessionmaker,
        actor_user_id=None,
    )

    slack_calls: list[str] = []
    webhook_calls: list[str] = []
    monkeypatch.setattr(dispatch, "send_slack", lambda url, text: slack_calls.append(url))
    monkeypatch.setattr(
        dispatch, "send_webhook", lambda url, payload: webhook_calls.append(url)
    )

    result = await dispatch.dispatch_alerts(
        {},
        org_id=str(org_id),
        project_id=str(project_1),
        issue_id=str(uuid.uuid4()),
        kind="new",
        title="Boom",
        level="error",
        session_factory=app_sessionmaker,
    )

    # org-wide slack + project-1 webhook fired; project-2 and disabled did not.
    assert slack_calls == ["https://h.example.com/orgwide"]
    assert webhook_calls == ["https://h.example.com/proj1"]
    assert result["delivered"] == 2
    assert result["failed"] == 0


async def test_dispatch_isolates_one_failing_channel(
    superuser_engine, app_sessionmaker, dispatch_org, monkeypatch
) -> None:
    org_id = dispatch_org["org_id"]
    project_1 = dispatch_org["project_1"]

    await alerts.create_channel(
        org_id, "slack", {"webhook_url": "https://h.example.com/good"}, None,
        session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    await alerts.create_channel(
        org_id, "slack", {"webhook_url": "https://h.example.com/bad"}, None,
        session_factory=app_sessionmaker,
        actor_user_id=None,
    )

    delivered: list[str] = []

    def flaky_slack(url: str, text: str) -> None:
        if url.endswith("/bad"):
            raise RuntimeError("simulated delivery failure")
        delivered.append(url)

    monkeypatch.setattr(dispatch, "send_slack", flaky_slack)

    result = await dispatch.dispatch_alerts(
        {},
        org_id=str(org_id),
        project_id=str(project_1),
        issue_id=str(uuid.uuid4()),
        kind="new",
        title="Boom",
        level="error",
        session_factory=app_sessionmaker,
    )

    # The good channel still delivered even though the bad one raised.
    assert delivered == ["https://h.example.com/good"]
    assert result["delivered"] == 1
    assert result["failed"] == 1
