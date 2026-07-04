"""Integration tests for the audit log slice (marked ``db``).

Like the sibling integration suites these require a live PostgreSQL with the
migrations applied and SKIP cleanly when none is reachable, so ``pytest -q``
passes locally without Postgres. In CI the postgres:16 service is up, the
migrations are applied, and these run for real.

Proves:

* every instrumented action (project/key/sampling/channel/member/issue) writes
  EXACTLY one ``audit_log`` row with the right action name and actor;
* ``invite.accepted`` records the ACCEPTING user as actor (not the admin who
  sent the invite);
* the design invariant that the audited mutation and its audit row commit or
  roll back TOGETHER, from both directions: a no-op action writes zero rows,
  and a failing audit write (a bad actor FK) rolls back the mutation it would
  have accompanied;
* the read API (``GET /orgs/{org_id}/audit-log``) is admin only, paginates
  newest-first, supports the exact-match ``action`` filter, resolves
  ``actor_email``, and is cross-org isolated (an ``isolation``-marked test).
"""

import datetime
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import accounts, alerts, audit, issues, projects, security
from app.config import get_settings
from app.jobs.process_event import process_event
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
        pytest.skip("PostgreSQL not reachable; skipping audit integration tests")
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


@pytest_asyncio.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_org(conn, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO orgs (id, name, slug) VALUES (:id, :name, :slug)"),
        {"id": org_id, "name": name, "slug": f"{name}-{org_id}"},
    )
    return org_id


async def _seed_user(conn, email: str, password: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO users (id, email, password_hash) VALUES (:id, :email, :ph)"),
        {"id": user_id, "email": email, "ph": security.hash_password(password)},
    )
    return user_id


async def _add_membership(conn, org_id, user_id, role) -> None:
    await conn.execute(
        text(
            "INSERT INTO org_memberships (org_id, user_id, role) "
            "VALUES (:oid, :uid, :role)"
        ),
        {"oid": org_id, "uid": user_id, "role": role},
    )


async def _audit_rows(superuser_engine, org_id) -> list:
    async with superuser_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT action, actor_user_id, target_type, target_id, data, "
                    "created_at FROM audit_log WHERE org_id = :oid"
                ),
                {"oid": org_id},
            )
        ).all()
    return rows


def _exc_envelope(event_id: str) -> dict:
    return {
        "event_id": event_id,
        "timestamp": "2026-07-04T12:00:00.000Z",
        "platform": "python",
        "level": "error",
        "environment": "production",
        "release": "web@1.0.0",
        "sdk": {"name": "crashlens-python", "version": "0.1.0"},
        "exception": {
            "type": "ValueError",
            "value": "audit trail test",
            "stacktrace": {
                "frames": [
                    {"filename": "app.py", "function": "handler", "lineno": 1, "in_app": True}
                ]
            },
        },
    }


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


# --- Every instrumented action writes exactly one row with action + actor -----
async def test_instrumented_actions_each_write_one_row_with_actor(
    app_sessionmaker, superuser_engine
) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "AuditFullCo")
        admin_id = await _seed_user(
            conn, f"admin-{uuid.uuid4()}@example.test", _KNOWN_PASSWORD
        )
        await _add_membership(conn, org_id, admin_id, "admin")
    try:
        project = await projects.create_project(
            org_id, "Payments API", "python",
            actor_user_id=admin_id, session_factory=app_sessionmaker,
        )
        assert project is not None

        key = await projects.create_dsn_key(
            org_id, project["id"], actor_user_id=admin_id, session_factory=app_sessionmaker
        )
        assert key is not None
        assert await projects.revoke_dsn_key(
            org_id, project["id"], key["id"],
            actor_user_id=admin_id, session_factory=app_sessionmaker,
        )

        assert await projects.update_project_sampling(
            org_id, project["id"], 0.5,
            actor_user_id=admin_id, session_factory=app_sessionmaker,
        )

        channel = await alerts.create_channel(
            org_id, "slack",
            {"webhook_url": "https://hooks.slack.com/services/A/B/C"}, None,
            actor_user_id=admin_id, session_factory=app_sessionmaker,
        )
        assert channel is not None
        assert await alerts.update_channel(
            org_id, channel["id"], enabled=False,
            actor_user_id=admin_id, session_factory=app_sessionmaker,
        )
        assert await alerts.delete_channel(
            org_id, channel["id"], actor_user_id=admin_id, session_factory=app_sessionmaker
        )

        invite, _raw_token = await accounts.create_invite(
            org_id, "invitee@example.test", "member",
            actor_user_id=admin_id, session_factory=app_sessionmaker,
        )
        assert invite is not None

        seeded = await process_event(
            {},
            envelope=_exc_envelope(str(uuid.uuid4())),
            org_id=str(org_id),
            project_id=str(project["id"]),
            dsn_key_id=str(uuid.uuid4()),
            received_at=_now_iso(),
            session_factory=app_sessionmaker,
        )
        issue_id = uuid.UUID(seeded["issue_id"])

        assert await issues.set_issue_status(
            org_id, project["id"], issue_id, "resolve",
            actor_user_id=admin_id, session_factory=app_sessionmaker,
        )
        assert await issues.set_issue_status(
            org_id, project["id"], issue_id, "reopen",
            actor_user_id=admin_id, session_factory=app_sessionmaker,
        )
        assert await issues.set_issue_status(
            org_id, project["id"], issue_id, "ignore",
            actor_user_id=admin_id, session_factory=app_sessionmaker,
        )
        assert await issues.assign_issue(
            org_id, project["id"], issue_id, admin_id,
            actor_user_id=admin_id, session_factory=app_sessionmaker,
        )
        assert await issues.delete_issue(
            org_id, project["id"], issue_id,
            actor_user_id=admin_id, session_factory=app_sessionmaker,
        )

        rows = await _audit_rows(superuser_engine, org_id)
        by_action: dict[str, list] = {}
        for row in rows:
            by_action.setdefault(row.action, []).append(row)

        expected_actions = [
            "project.created",
            "key.created",
            "key.revoked",
            "sampling.updated",
            "channel.created",
            "channel.updated",
            "channel.deleted",
            "member.invited",
            "issue.resolved",
            "issue.reopened",
            "issue.ignored",
            "issue.assigned",
            "issue.deleted",
        ]
        for action in expected_actions:
            assert action in by_action, f"missing audit row for {action}"
            assert len(by_action[action]) == 1, f"expected exactly one {action} row"
            assert by_action[action][0].actor_user_id == admin_id

        # No stray rows beyond the expected set.
        assert set(by_action) == set(expected_actions)
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM events WHERE org_id = :o"), {"o": org_id}
            )
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": admin_id})


async def test_invite_accept_audits_the_accepting_user_as_actor(
    app_sessionmaker, superuser_engine
) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "InviteAuditCo")
        admin_id = await _seed_user(
            conn, f"admin-{uuid.uuid4()}@example.test", _KNOWN_PASSWORD
        )
        await _add_membership(conn, org_id, admin_id, "admin")
    accepting_user_id = None
    try:
        email = f"newbie-{uuid.uuid4()}@example.test"
        invite, raw_token = await accounts.create_invite(
            org_id, email, "member",
            actor_user_id=admin_id, session_factory=app_sessionmaker,
        )
        result = await accounts.accept_invite(
            raw_token, email, _KNOWN_PASSWORD, session_factory=app_sessionmaker
        )
        assert result is not None
        accepting_user_id = result["user_id"]

        rows = await _audit_rows(superuser_engine, org_id)
        accepted = [r for r in rows if r.action == "invite.accepted"]
        assert len(accepted) == 1
        assert accepted[0].actor_user_id == accepting_user_id
        # The invite-sending admin is a DIFFERENT actor than the one who accepted.
        assert accepted[0].actor_user_id != admin_id
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": admin_id})
            if accepting_user_id is not None:
                await conn.execute(
                    text("DELETE FROM users WHERE id = :id"), {"id": accepting_user_id}
                )


# --- Atomicity: mutation and audit row commit or roll back together -----------
async def test_no_op_action_writes_zero_audit_rows(
    app_sessionmaker, superuser_engine
) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "NoOpAuditCo")
        admin_id = await _seed_user(
            conn, f"admin-{uuid.uuid4()}@example.test", _KNOWN_PASSWORD
        )
    try:
        deleted = await projects.delete_project(
            org_id, uuid.uuid4(), actor_user_id=admin_id, session_factory=app_sessionmaker
        )
        assert deleted is False
        assert await _audit_rows(superuser_engine, org_id) == []
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": admin_id})


async def test_audit_write_failure_rolls_back_the_paired_mutation(
    app_sessionmaker, superuser_engine
) -> None:
    """A bad ``actor_user_id`` (no such user) fails the audit INSERT's FK.

    Because ``audit.record`` runs in the SAME transaction as the mutation it
    documents, that FK failure rolls back the ENTIRE transaction -- the project
    row included. ``create_project``'s broad ``except IntegrityError`` reports
    this the same way it reports a slug clash: a uniform ``None``. Proving the
    project was never persisted is the direct evidence for the module's
    documented "commit or roll back together" design invariant.
    """
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "RollbackAuditCo")
    try:
        bogus_actor = uuid.uuid4()  # no users row exists with this id
        created = await projects.create_project(
            org_id, "Ghost Project", None,
            actor_user_id=bogus_actor, session_factory=app_sessionmaker,
        )
        assert created is None
        listed = await projects.list_projects(org_id, session_factory=app_sessionmaker)
        assert listed == []
        assert await _audit_rows(superuser_engine, org_id) == []
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})


# --- Read API: admin-only, paginated, filterable, cross-org isolated ----------
async def test_audit_log_endpoint_is_admin_only(superuser_engine, client) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "AuditReadCo")
        admin_id = await _seed_user(
            conn, f"raadmin-{uuid.uuid4()}@example.test", _KNOWN_PASSWORD
        )
        member_id = await _seed_user(
            conn, f"ramember-{uuid.uuid4()}@example.test", _KNOWN_PASSWORD
        )
        await _add_membership(conn, org_id, admin_id, "admin")
        await _add_membership(conn, org_id, member_id, "member")
    admin_h = {"Authorization": f"Bearer {security.create_access_token(admin_id)}"}
    member_h = {"Authorization": f"Bearer {security.create_access_token(member_id)}"}
    try:
        url = f"/orgs/{org_id}/audit-log"
        assert (await client.get(url, headers=member_h)).status_code == 403
        r = await client.get(url, headers=admin_h)
        assert r.status_code == 200
        body = r.json()
        assert body["entries"] == []
        assert body["total"] == 0
        assert body["page"] == 1
        assert body["per_page"] == audit.DEFAULT_PER_PAGE
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})
            await conn.execute(
                text("DELETE FROM users WHERE id IN (:a, :m)"),
                {"a": admin_id, "m": member_id},
            )


async def test_audit_log_pagination_and_action_filter_and_actor_email(
    app_sessionmaker, superuser_engine, client
) -> None:
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "PagerAuditCo")
        admin_email = f"pgadmin-{uuid.uuid4()}@example.test"
        admin_id = await _seed_user(conn, admin_email, _KNOWN_PASSWORD)
        await _add_membership(conn, org_id, admin_id, "admin")
    admin_h = {"Authorization": f"Bearer {security.create_access_token(admin_id)}"}
    try:
        for i in range(3):
            created = await projects.create_project(
                org_id, f"Page Project {i}", None,
                actor_user_id=admin_id, session_factory=app_sessionmaker,
            )
            assert created is not None

        url = f"/orgs/{org_id}/audit-log"
        page1 = await client.get(url, params={"page": 1, "per_page": 2}, headers=admin_h)
        assert page1.status_code == 200
        body1 = page1.json()
        assert body1["total"] == 3
        assert len(body1["entries"]) == 2

        page2 = await client.get(url, params={"page": 2, "per_page": 2}, headers=admin_h)
        body2 = page2.json()
        assert len(body2["entries"]) == 1

        ids1 = {e["id"] for e in body1["entries"]}
        ids2 = {e["id"] for e in body2["entries"]}
        assert ids1.isdisjoint(ids2)

        # Newest first: the most recently created project leads page 1.
        assert body1["entries"][0]["data"]["name"] == "Page Project 2"
        # Actor email is resolved from the RLS-exempt users table.
        assert body1["entries"][0]["actor_email"] == admin_email

        # Exact-match action filter.
        matching = await client.get(
            url, params={"action": "project.created"}, headers=admin_h
        )
        assert matching.json()["total"] == 3
        no_match = await client.get(
            url, params={"action": "issue.deleted"}, headers=admin_h
        )
        assert no_match.json()["total"] == 0
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": admin_id})


@pytest.mark.isolation
async def test_audit_log_cross_org_isolation(
    app_sessionmaker, superuser_engine, client
) -> None:
    async with superuser_engine.begin() as conn:
        org_a = await _seed_org(conn, "AuditOrgA")
        org_b = await _seed_org(conn, "AuditOrgB")
        admin_a = await _seed_user(conn, f"aa-{uuid.uuid4()}@example.test", _KNOWN_PASSWORD)
        admin_b = await _seed_user(conn, f"ab-{uuid.uuid4()}@example.test", _KNOWN_PASSWORD)
        await _add_membership(conn, org_a, admin_a, "admin")
        await _add_membership(conn, org_b, admin_b, "admin")
    admin_a_h = {"Authorization": f"Bearer {security.create_access_token(admin_a)}"}
    try:
        assert (
            await projects.create_project(
                org_a, "A Secret Project", None,
                actor_user_id=admin_a, session_factory=app_sessionmaker,
            )
        ) is not None
        assert (
            await projects.create_project(
                org_b, "B Project", None,
                actor_user_id=admin_b, session_factory=app_sessionmaker,
            )
        ) is not None

        # HTTP-level: org A's own admin token against org B's audit-log path.
        cross = await client.get(f"/orgs/{org_b}/audit-log", headers=admin_a_h)
        assert cross.status_code == 403

        # DB-level: an RLS-scoped read for org A sees only org A's own row.
        result = await audit.list_audit_log(org_a, session_factory=app_sessionmaker)
        assert result["total"] == 1
        assert result["entries"][0]["data"]["name"] == "A Secret Project"
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM orgs WHERE id IN (:a, :b)"),
                {"a": org_a, "b": org_b},
            )
            await conn.execute(
                text("DELETE FROM users WHERE id IN (:a, :b)"),
                {"a": admin_a, "b": admin_b},
            )
