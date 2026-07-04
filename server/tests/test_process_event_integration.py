"""DB-marked integration tests for the process_event consumer.

These call ``process_event`` DIRECTLY with a fake ``ctx`` (no Redis needed) and
inject a session factory bound to the NON-superuser ``crashlens_test`` role so
RLS is genuinely enforced -- mirroring ``test_retention_jobs.py`` and
``test_db_integration.py``. They require a live PostgreSQL with migration 0001
applied and SKIP cleanly when none is reachable, so ``pytest -q`` passes locally
without Postgres.

Proved here: first event creates Issue + event row; a second distinct event on
the same trace increments the Issue counter without creating a second Issue; a
duplicate ``(project_id, event_id)`` is a no-op; a new event on a resolved Issue
flips it to regressed; an ignored Issue stays ignored; and an event for org A is
invisible to org B's tenant_session (cross-org isolation).
"""

import datetime
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db import tenant_session
from app.jobs.process_event import compute_fingerprint, normalize_envelope, process_event

pytestmark = pytest.mark.db

_TEST_ROLE = "crashlens_test"
_TEST_PASSWORD = "crashlens_test"


@pytest_asyncio.fixture(scope="module")
async def superuser_engine():
    """Engine using the migration/superuser DATABASE_URL. Skips if unreachable."""
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("PostgreSQL not reachable; skipping process_event integration tests")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="module")
async def app_sessionmaker(superuser_engine):
    """Session factory bound to a non-superuser role so RLS actually applies.

    Same shape (role name, idempotent creation, grants) as the sibling
    integration tests so all can share one CI session without conflict.
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

    url = make_url(get_settings().database_url).set(
        username=_TEST_ROLE, password=_TEST_PASSWORD
    )
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def two_orgs(superuser_engine):
    """Seed two orgs, each with one project. Cleaned up on teardown."""
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    project_a, project_b = uuid.uuid4(), uuid.uuid4()
    async with superuser_engine.begin() as conn:
        for org, slug in ((org_a, "pe-org-a"), (org_b, "pe-org-b")):
            await conn.execute(
                text("INSERT INTO orgs (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": org, "name": slug, "slug": f"{slug}-{org}"},
            )
        for project, org, slug in (
            (project_a, org_a, "pe-proj-a"),
            (project_b, org_b, "pe-proj-b"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO projects (id, org_id, name, slug, platform) "
                    "VALUES (:id, :org, :name, :slug, 'python')"
                ),
                {"id": project, "org": org, "name": slug, "slug": slug},
            )
    yield {"org_a": org_a, "org_b": org_b, "project_a": project_a, "project_b": project_b}
    async with superuser_engine.begin() as conn:
        # events has no FK to orgs (raw partitioned table), so delete it
        # explicitly before the org cascade removes projects/issues.
        await conn.execute(
            text("DELETE FROM events WHERE org_id IN (:a, :b)"),
            {"a": org_a, "b": org_b},
        )
        await conn.execute(
            text("DELETE FROM orgs WHERE id IN (:a, :b)"), {"a": org_a, "b": org_b}
        )


def _exc_envelope(event_id: str) -> dict:
    """A stable exception envelope (one fingerprint) with a given event_id."""
    return {
        "event_id": event_id,
        "timestamp": "2026-07-04T12:00:00.000Z",
        "platform": "python",
        "level": "error",
        "environment": "production",
        "release": "web@1.0.0",
        "sdk": {"name": "crashlens-python", "version": "0.1.0"},
        "exception": {
            "type": "ZeroDivisionError",
            "value": "division by zero",
            "stacktrace": {
                "frames": [
                    {"filename": "billing.py", "function": "compute_total", "in_app": True}
                ]
            },
        },
    }


def _now_iso() -> str:
    # Land in today's partition (migration seeds today..+7).
    return datetime.datetime.now(datetime.UTC).isoformat()


async def _issue_row(app_sessionmaker, org_id, project_id, fingerprint):
    async with tenant_session(str(org_id), session_factory=app_sessionmaker) as session:
        return (
            await session.execute(
                text(
                    "SELECT id, status, event_count, title FROM issues "
                    "WHERE project_id = :p AND fingerprint = :f"
                ),
                {"p": project_id, "f": fingerprint},
            )
        ).one()


@pytest.mark.isolation
async def test_first_event_creates_issue_and_event_row(app_sessionmaker, two_orgs) -> None:
    org_a = two_orgs["org_a"]
    project_a = two_orgs["project_a"]
    envelope = _exc_envelope(str(uuid.uuid4()))
    fingerprint = compute_fingerprint(normalize_envelope(envelope))

    result = await process_event(
        {},
        envelope=envelope,
        org_id=str(org_a),
        project_id=str(project_a),
        dsn_key_id=str(uuid.uuid4()),
        received_at=_now_iso(),
        session_factory=app_sessionmaker,
    )
    assert result["created"] is True
    assert result["regressed"] is False
    assert result["event_count"] == 1

    row = await _issue_row(app_sessionmaker, org_a, project_a, fingerprint)
    assert row.status == "unresolved"
    assert row.event_count == 1
    assert row.title == "ZeroDivisionError: division by zero"

    async with tenant_session(str(org_a), session_factory=app_sessionmaker) as session:
        event_count = (
            await session.execute(
                text("SELECT count(*) FROM events WHERE event_id = :e"),
                {"e": envelope["event_id"]},
            )
        ).scalar_one()
        assert event_count == 1


async def test_second_distinct_event_same_trace_increments_not_duplicates(
    app_sessionmaker, two_orgs
) -> None:
    org_a = two_orgs["org_a"]
    project_a = two_orgs["project_a"]
    env1 = _exc_envelope(str(uuid.uuid4()))
    env2 = _exc_envelope(str(uuid.uuid4()))  # same trace, different event_id
    fingerprint = compute_fingerprint(normalize_envelope(env1))

    for env in (env1, env2):
        await process_event(
            {},
            envelope=env,
            org_id=str(org_a),
            project_id=str(project_a),
            dsn_key_id=str(uuid.uuid4()),
            received_at=_now_iso(),
            session_factory=app_sessionmaker,
        )

    # One Issue, event_count == 2.
    async with tenant_session(str(org_a), session_factory=app_sessionmaker) as session:
        issue_count = (
            await session.execute(
                text("SELECT count(*) FROM issues WHERE project_id = :p AND fingerprint = :f"),
                {"p": project_a, "f": fingerprint},
            )
        ).scalar_one()
        assert issue_count == 1
    row = await _issue_row(app_sessionmaker, org_a, project_a, fingerprint)
    assert row.event_count == 2


async def test_duplicate_event_id_is_a_noop(app_sessionmaker, two_orgs) -> None:
    org_a = two_orgs["org_a"]
    project_a = two_orgs["project_a"]
    envelope = _exc_envelope(str(uuid.uuid4()))
    fingerprint = compute_fingerprint(normalize_envelope(envelope))
    args = dict(
        org_id=str(org_a),
        project_id=str(project_a),
        dsn_key_id=str(uuid.uuid4()),
        received_at=_now_iso(),
        session_factory=app_sessionmaker,
    )

    first = await process_event({}, envelope=envelope, **args)
    assert first["created"] is True
    # Same event_id again (a client resend) must be a no-op.
    second = await process_event({}, envelope=envelope, **args)
    assert second["status"] == "duplicate"
    assert second["issue_id"] is None

    row = await _issue_row(app_sessionmaker, org_a, project_a, fingerprint)
    assert row.event_count == 1  # not double-counted
    async with tenant_session(str(org_a), session_factory=app_sessionmaker) as session:
        stored = (
            await session.execute(
                text("SELECT count(*) FROM events WHERE event_id = :e"),
                {"e": envelope["event_id"]},
            )
        ).scalar_one()
        assert stored == 1


async def test_resolved_issue_regresses_on_new_event(app_sessionmaker, two_orgs) -> None:
    org_a = two_orgs["org_a"]
    project_a = two_orgs["project_a"]
    env1 = _exc_envelope(str(uuid.uuid4()))
    fingerprint = compute_fingerprint(normalize_envelope(env1))
    args = dict(
        org_id=str(org_a),
        project_id=str(project_a),
        dsn_key_id=str(uuid.uuid4()),
        session_factory=app_sessionmaker,
    )
    await process_event({}, envelope=env1, received_at=_now_iso(), **args)

    # Resolve the Issue (as the app role, RLS-scoped).
    async with tenant_session(str(org_a), session_factory=app_sessionmaker) as session:
        await session.execute(
            text(
                "UPDATE issues SET status = 'resolved' "
                "WHERE project_id = :p AND fingerprint = :f"
            ),
            {"p": project_a, "f": fingerprint},
        )

    # A new event on the same trace flips resolved -> regressed.
    env2 = _exc_envelope(str(uuid.uuid4()))
    result = await process_event({}, envelope=env2, received_at=_now_iso(), **args)
    assert result["created"] is False
    assert result["regressed"] is True
    assert result["status"] == "regressed"

    row = await _issue_row(app_sessionmaker, org_a, project_a, fingerprint)
    assert row.status == "regressed"
    assert row.event_count == 2


async def test_ignored_issue_stays_ignored(app_sessionmaker, two_orgs) -> None:
    org_a = two_orgs["org_a"]
    project_a = two_orgs["project_a"]
    env1 = _exc_envelope(str(uuid.uuid4()))
    fingerprint = compute_fingerprint(normalize_envelope(env1))
    args = dict(
        org_id=str(org_a),
        project_id=str(project_a),
        dsn_key_id=str(uuid.uuid4()),
        session_factory=app_sessionmaker,
    )
    await process_event({}, envelope=env1, received_at=_now_iso(), **args)

    async with tenant_session(str(org_a), session_factory=app_sessionmaker) as session:
        await session.execute(
            text(
                "UPDATE issues SET status = 'ignored' "
                "WHERE project_id = :p AND fingerprint = :f"
            ),
            {"p": project_a, "f": fingerprint},
        )

    env2 = _exc_envelope(str(uuid.uuid4()))
    result = await process_event({}, envelope=env2, received_at=_now_iso(), **args)
    assert result["status"] == "ignored"
    assert result["regressed"] is False

    row = await _issue_row(app_sessionmaker, org_a, project_a, fingerprint)
    assert row.status == "ignored"
    assert row.event_count == 2  # still counted


@pytest.mark.isolation
async def test_cross_org_event_isolation(app_sessionmaker, two_orgs) -> None:
    org_a = two_orgs["org_a"]
    org_b = two_orgs["org_b"]
    project_a = two_orgs["project_a"]
    envelope = _exc_envelope(str(uuid.uuid4()))

    await process_event(
        {},
        envelope=envelope,
        org_id=str(org_a),
        project_id=str(project_a),
        dsn_key_id=str(uuid.uuid4()),
        received_at=_now_iso(),
        session_factory=app_sessionmaker,
    )

    # org B's scope sees neither the event nor the Issue org A just created.
    async with tenant_session(str(org_b), session_factory=app_sessionmaker) as session:
        seen_event = (
            await session.execute(
                text("SELECT count(*) FROM events WHERE event_id = :e"),
                {"e": envelope["event_id"]},
            )
        ).scalar_one()
        seen_issue = (
            await session.execute(
                text("SELECT count(*) FROM issues WHERE project_id = :p"),
                {"p": project_a},
            )
        ).scalar_one()
    assert seen_event == 0
    assert seen_issue == 0

    # org A's own scope sees both.
    async with tenant_session(str(org_a), session_factory=app_sessionmaker) as session:
        seen_event_a = (
            await session.execute(
                text("SELECT count(*) FROM events WHERE event_id = :e"),
                {"e": envelope["event_id"]},
            )
        ).scalar_one()
        assert seen_event_a == 1
