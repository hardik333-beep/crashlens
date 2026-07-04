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

from app import issues
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


# ==============================================================================
# W5-02: release tracking + release-aware regression.
# ==============================================================================


class _RecordingPool:
    """Fake arq redis pool: records enqueue_job calls so the regression alert
    signal (kind + release) can be asserted without a real Redis."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict]] = []

    async def enqueue_job(self, name: str, **kwargs) -> None:
        self.jobs.append((name, kwargs))


def _envelope_with_release(event_id: str, release: str | None) -> dict:
    """A stable exception envelope with the release overridden (or removed)."""
    env = _exc_envelope(event_id)
    if release is None:
        env.pop("release", None)
    else:
        env["release"] = release
    return env


async def _process(app_sessionmaker, org_id, project_id, envelope, ctx=None) -> dict:
    return await process_event(
        ctx if ctx is not None else {},
        envelope=envelope,
        org_id=str(org_id),
        project_id=str(project_id),
        dsn_key_id=str(uuid.uuid4()),
        received_at=_now_iso(),
        session_factory=app_sessionmaker,
    )


async def _release_row_created_at(app_sessionmaker, org_id, project_id, version):
    async with tenant_session(str(org_id), session_factory=app_sessionmaker) as session:
        return (
            await session.execute(
                text(
                    "SELECT created_at FROM releases "
                    "WHERE project_id = :p AND version = :v"
                ),
                {"p": project_id, "v": version},
            )
        ).scalar_one_or_none()


async def _issue_release_fields(app_sessionmaker, org_id, project_id, fingerprint):
    async with tenant_session(str(org_id), session_factory=app_sessionmaker) as session:
        return (
            await session.execute(
                text(
                    "SELECT status, resolved_in_release, regressed_in_release "
                    "FROM issues WHERE project_id = :p AND fingerprint = :f"
                ),
                {"p": project_id, "f": fingerprint},
            )
        ).one()


async def test_release_upsert_dedupes(app_sessionmaker, two_orgs) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    # Two distinct events carrying the SAME release string.
    for _ in range(2):
        await _process(
            app_sessionmaker, org_a, project_a,
            _envelope_with_release(str(uuid.uuid4()), "web@1.0.0"),
        )
    async with tenant_session(str(org_a), session_factory=app_sessionmaker) as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM releases "
                    "WHERE project_id = :p AND version = :v"
                ),
                {"p": project_a, "v": "web@1.0.0"},
            )
        ).scalar_one()
    assert count == 1  # ON CONFLICT DO NOTHING deduped the second insert.


async def test_resolve_captures_latest_release(
    app_sessionmaker, superuser_engine, two_orgs
) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    res = await _process(
        app_sessionmaker, org_a, project_a,
        _envelope_with_release(str(uuid.uuid4()), "web@1.0.0"),
    )
    issue_id = uuid.UUID(res["issue_id"])
    # Seed a strictly-newer release so "latest" is unambiguous (deterministic
    # created_at rather than relying on wall-clock spacing between transactions).
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO releases (org_id, project_id, version, created_at) "
                "VALUES (:o, :p, :v, now() + interval '1 hour')"
            ),
            {"o": org_a, "p": project_a, "v": "web@2.0.0"},
        )
    resolved = await issues.set_issue_status(
        org_a, project_a, issue_id, "resolve", session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    assert resolved is not None
    assert resolved["status"] == "resolved"
    assert resolved["resolved_in_release"] == "web@2.0.0"


async def test_resolve_with_no_releases_records_null(
    app_sessionmaker, superuser_engine, two_orgs
) -> None:
    # A project with releases but an issue seeded from a no-release event: resolve
    # still records the project's latest release. Here the project has NONE, so
    # resolved_in_release is NULL.
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    res = await _process(
        app_sessionmaker, org_a, project_a,
        _envelope_with_release(str(uuid.uuid4()), None),
    )
    issue_id = uuid.UUID(res["issue_id"])
    resolved = await issues.set_issue_status(
        org_a, project_a, issue_id, "resolve", session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    assert resolved is not None
    assert resolved["resolved_in_release"] is None


async def test_same_release_event_does_not_regress(
    app_sessionmaker, two_orgs
) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    env1 = _envelope_with_release(str(uuid.uuid4()), "web@1.0.0")
    fingerprint = compute_fingerprint(normalize_envelope(env1))
    res = await _process(app_sessionmaker, org_a, project_a, env1)
    issue_id = uuid.UUID(res["issue_id"])
    # Resolve: captures web@1.0.0 (the only release) as the fix release.
    resolved = await issues.set_issue_status(
        org_a, project_a, issue_id, "resolve", session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    assert resolved is not None and resolved["resolved_in_release"] == "web@1.0.0"

    # A new event on the SAME release must NOT regress (still counts).
    result = await _process(
        app_sessionmaker, org_a, project_a,
        _envelope_with_release(str(uuid.uuid4()), "web@1.0.0"),
    )
    assert result["regressed"] is False
    assert result["status"] == "resolved"
    row = await _issue_release_fields(app_sessionmaker, org_a, project_a, fingerprint)
    assert row.status == "resolved"
    assert row.regressed_in_release is None


async def test_newer_release_regresses_records_and_signals(
    app_sessionmaker, superuser_engine, two_orgs
) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    env1 = _envelope_with_release(str(uuid.uuid4()), "web@1.0.0")
    fingerprint = compute_fingerprint(normalize_envelope(env1))
    res = await _process(app_sessionmaker, org_a, project_a, env1)
    issue_id = uuid.UUID(res["issue_id"])
    resolved = await issues.set_issue_status(
        org_a, project_a, issue_id, "resolve", session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    assert resolved is not None and resolved["resolved_in_release"] == "web@1.0.0"

    # Seed a strictly-newer release AFTER the resolve (so the fix stays 1.0.0).
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO releases (org_id, project_id, version, created_at) "
                "VALUES (:o, :p, :v, now() + interval '1 hour')"
            ),
            {"o": org_a, "p": project_a, "v": "web@2.0.0"},
        )

    # An event from the NEWER release regresses and records the came-back release,
    # and dispatches a regression alert signal (kind + release) via the pool.
    pool = _RecordingPool()
    result = await _process(
        app_sessionmaker, org_a, project_a,
        _envelope_with_release(str(uuid.uuid4()), "web@2.0.0"),
        ctx={"redis": pool},
    )
    assert result["regressed"] is True
    assert result["status"] == "regressed"
    assert result["regressed_in_release"] == "web@2.0.0"

    row = await _issue_release_fields(app_sessionmaker, org_a, project_a, fingerprint)
    assert row.status == "regressed"
    assert row.regressed_in_release == "web@2.0.0"
    # The fix release is untouched by the regression.
    assert row.resolved_in_release == "web@1.0.0"

    assert len(pool.jobs) == 1
    name, kwargs = pool.jobs[0]
    assert name == "dispatch_alerts"
    assert kwargs["kind"] == "regression"
    assert kwargs["release"] == "web@2.0.0"


async def test_no_release_event_regresses_rule_b(
    app_sessionmaker, two_orgs
) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    env1 = _envelope_with_release(str(uuid.uuid4()), "web@1.0.0")
    fingerprint = compute_fingerprint(normalize_envelope(env1))
    res = await _process(app_sessionmaker, org_a, project_a, env1)
    issue_id = uuid.UUID(res["issue_id"])
    resolved = await issues.set_issue_status(
        org_a, project_a, issue_id, "resolve", session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    assert resolved is not None and resolved["resolved_in_release"] == "web@1.0.0"

    # An event with NO release regresses (rule b), recording NULL as the release.
    result = await _process(
        app_sessionmaker, org_a, project_a,
        _envelope_with_release(str(uuid.uuid4()), None),
    )
    assert result["regressed"] is True
    assert result["status"] == "regressed"
    assert result["regressed_in_release"] is None
    row = await _issue_release_fields(app_sessionmaker, org_a, project_a, fingerprint)
    assert row.status == "regressed"
    assert row.regressed_in_release is None


async def test_reopen_clears_both_release_fields(
    app_sessionmaker, superuser_engine, two_orgs
) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    env1 = _envelope_with_release(str(uuid.uuid4()), "web@1.0.0")
    fingerprint = compute_fingerprint(normalize_envelope(env1))
    res = await _process(app_sessionmaker, org_a, project_a, env1)
    issue_id = uuid.UUID(res["issue_id"])
    await issues.set_issue_status(
        org_a, project_a, issue_id, "resolve", session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO releases (org_id, project_id, version, created_at) "
                "VALUES (:o, :p, :v, now() + interval '1 hour')"
            ),
            {"o": org_a, "p": project_a, "v": "web@2.0.0"},
        )
    # Drive it into a regressed state with both release fields set.
    await _process(
        app_sessionmaker, org_a, project_a,
        _envelope_with_release(str(uuid.uuid4()), "web@2.0.0"),
    )
    before = await _issue_release_fields(app_sessionmaker, org_a, project_a, fingerprint)
    assert before.status == "regressed"
    assert before.resolved_in_release == "web@1.0.0"
    assert before.regressed_in_release == "web@2.0.0"

    # Reopen clears BOTH release fields and restores unresolved.
    reopened = await issues.set_issue_status(
        org_a, project_a, issue_id, "reopen", session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    assert reopened is not None
    assert reopened["status"] == "unresolved"
    assert reopened["resolved_in_release"] is None
    assert reopened["regressed_in_release"] is None


async def test_ignore_clears_regressed_release_keeps_fix(
    app_sessionmaker, superuser_engine, two_orgs
) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    env1 = _envelope_with_release(str(uuid.uuid4()), "web@1.0.0")
    fingerprint = compute_fingerprint(normalize_envelope(env1))
    res = await _process(app_sessionmaker, org_a, project_a, env1)
    issue_id = uuid.UUID(res["issue_id"])
    await issues.set_issue_status(
        org_a, project_a, issue_id, "resolve", session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO releases (org_id, project_id, version, created_at) "
                "VALUES (:o, :p, :v, now() + interval '1 hour')"
            ),
            {"o": org_a, "p": project_a, "v": "web@2.0.0"},
        )
    await _process(
        app_sessionmaker, org_a, project_a,
        _envelope_with_release(str(uuid.uuid4()), "web@2.0.0"),
    )
    # Ignore clears the came-back release but keeps the recorded fix release.
    ignored = await issues.set_issue_status(
        org_a, project_a, issue_id, "ignore", session_factory=app_sessionmaker,
        actor_user_id=None,
    )
    assert ignored is not None
    assert ignored["status"] == "ignored"
    assert ignored["regressed_in_release"] is None
    assert ignored["resolved_in_release"] == "web@1.0.0"
    row = await _issue_release_fields(app_sessionmaker, org_a, project_a, fingerprint)
    assert row.regressed_in_release is None
