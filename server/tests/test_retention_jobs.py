"""Tests for the partition-maintenance and retention background jobs.

Pure-logic unit tests need no database and always run. The ``db``-marked
integration tests below require a live PostgreSQL with migration 0001 applied;
they SKIP cleanly when no database is reachable (same pattern as
``test_db_integration.py``) and connect through their own fixtures rather than
importing that file's, so this file has no cross-file coupling.
"""

import datetime
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db import tenant_session
from app.jobs.retention import (
    DEFAULT_RETENTION_DAYS_FALLBACK,
    _load_projects_by_org,
    _trim_over_retention_projects,
    compute_retention_cutoff,
    enforce_retention,
    fold_global_max_retention_days,
    lookahead_partition_days,
    maintain_event_partitions,
    partitions_older_than,
    projects_needing_trim,
)

_TEST_ROLE = "crashlens_test"
_TEST_PASSWORD = "crashlens_test"


# --------------------------------------------------------------------------
# Pure-logic unit tests: no database required.
# --------------------------------------------------------------------------


def test_lookahead_partition_days_is_today_through_plus_seven() -> None:
    today = datetime.date(2026, 7, 4)
    days = lookahead_partition_days(today)
    assert days == [today + datetime.timedelta(days=n) for n in range(8)]
    assert len(days) == 8
    assert days[0] == today
    assert days[-1] == today + datetime.timedelta(days=7)


def test_lookahead_partition_days_respects_custom_horizon() -> None:
    today = datetime.date(2026, 1, 1)
    assert lookahead_partition_days(today, days_ahead=0) == [today]
    assert len(lookahead_partition_days(today, days_ahead=2)) == 3


def test_compute_retention_cutoff_subtracts_global_max_days() -> None:
    today = datetime.date(2026, 7, 4)
    assert compute_retention_cutoff(today, 30) == datetime.date(2026, 6, 4)
    assert compute_retention_cutoff(today, 0) == today


def test_fold_global_max_retention_days_returns_max_when_present() -> None:
    assert fold_global_max_retention_days([30, 90, 7]) == 90
    assert fold_global_max_retention_days([14]) == 14


def test_fold_global_max_retention_days_falls_back_when_empty() -> None:
    assert fold_global_max_retention_days([]) == DEFAULT_RETENTION_DAYS_FALLBACK
    assert fold_global_max_retention_days([], fallback=42) == 42


def test_partitions_older_than_filters_by_parsed_day() -> None:
    names = ["events_20260101", "events_20260201", "events_20260301", "not_an_events_table"]
    cutoff = datetime.date(2026, 2, 15)
    assert partitions_older_than(names, cutoff) == ["events_20260101", "events_20260201"]


def test_partitions_older_than_is_idempotent_on_already_dropped_names() -> None:
    # Re-running against an empty (already-dropped) list is a no-op, matching
    # the SQL function's own idempotency.
    assert partitions_older_than([], datetime.date(2026, 1, 1)) == []


def test_projects_needing_trim_filters_below_global_ceiling() -> None:
    project_a, project_b, project_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    projects = [(project_a, 7), (project_b, 30), (project_c, 30)]
    assert projects_needing_trim(projects, global_max_retention_days=30) == [(project_a, 7)]


def test_projects_needing_trim_empty_when_all_at_ceiling() -> None:
    project_a = uuid.uuid4()
    assert projects_needing_trim([(project_a, 30)], global_max_retention_days=30) == []


# --------------------------------------------------------------------------
# DB-marked integration tests.
# --------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def superuser_engine():
    """Engine using the migration/superuser DATABASE_URL. Skips if unreachable."""
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("PostgreSQL not reachable; skipping retention job integration tests")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="module")
async def app_sessionmaker(superuser_engine):
    """Session factory bound to a non-superuser role so RLS actually applies.

    Mirrors ``test_db_integration.py``'s fixture of the same shape exactly
    (same role name, idempotent creation) so both files can run in the same
    CI session without conflict, but is defined independently here to keep
    this file free of any cross-file import coupling.
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
async def retention_two_orgs(superuser_engine):
    """Seed two orgs, each with one project at a different ``retention_days``.

    ``org_a``/``project_a`` retention_days=1 (will be trimmed); ``org_b``/
    ``project_b`` retention_days=30 (will not: 30 is always >= the global max
    folded from these two, since it IS one of the folded values, so it is
    never itself "below the ceiling"). Seeded via the superuser connection
    (bypasses RLS by design, same pattern as ``test_db_integration.py``).
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    project_a, project_b = uuid.uuid4(), uuid.uuid4()
    async with superuser_engine.begin() as conn:
        for org, slug in ((org_a, "ret-org-a"), (org_b, "ret-org-b")):
            await conn.execute(
                text("INSERT INTO orgs (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": org, "name": slug, "slug": f"{slug}-{org}"},
            )
        for project, org, slug, retention_days in (
            (project_a, org_a, "ret-proj-a", 1),
            (project_b, org_b, "ret-proj-b", 30),
        ):
            await conn.execute(
                text(
                    "INSERT INTO projects (id, org_id, name, slug, retention_days) "
                    "VALUES (:id, :org, :name, :slug, :retention_days)"
                ),
                {
                    "id": project,
                    "org": org,
                    "name": slug,
                    "slug": slug,
                    "retention_days": retention_days,
                },
            )
    yield {"org_a": org_a, "org_b": org_b, "project_a": project_a, "project_b": project_b}
    async with superuser_engine.begin() as conn:
        # Cascades projects (and any events/dsn_keys/etc for these orgs).
        await conn.execute(
            text("DELETE FROM orgs WHERE id IN (:a, :b)"), {"a": org_a, "b": org_b}
        )


@pytest.mark.db
async def test_maintain_event_partitions_creates_lookahead_and_is_idempotent(
    superuser_engine,
) -> None:
    today = datetime.date.today()
    expected_names = [f"events_{day.strftime('%Y%m%d')}" for day in lookahead_partition_days(today)]

    # First call: partitions should exist afterward (migration 0001 already
    # seeds today..+7 at deploy time, so this also covers "already created,
    # re-running is a no-op").
    await maintain_event_partitions({})
    async with superuser_engine.connect() as conn:
        for name in expected_names:
            exists = (
                await conn.execute(text(f"SELECT to_regclass('public.{name}') IS NOT NULL"))
            ).scalar_one()
            assert exists is True, f"expected partition {name} to exist"

    # Second call: idempotent, no error, partitions still present.
    await maintain_event_partitions({})
    async with superuser_engine.connect() as conn:
        for name in expected_names:
            exists = (
                await conn.execute(text(f"SELECT to_regclass('public.{name}') IS NOT NULL"))
            ).scalar_one()
            assert exists is True, f"expected partition {name} to still exist after re-run"


@pytest.mark.db
async def test_enforce_retention_partition_drop_removes_only_older_than_cutoff(
    superuser_engine,
) -> None:
    # Two far-apart, test-only partitions: one certain to be older than any
    # realistic cutoff (global_max is at most a few hundred days), one
    # certain to be newer (the far future). Selectivity is proven regardless
    # of what global_max_retention_days actually resolves to in this run.
    past_day = datetime.date(1900, 1, 1)
    future_day = datetime.date(2997, 6, 1)
    past_name = f"events_{past_day.strftime('%Y%m%d')}"
    future_name = f"events_{future_day.strftime('%Y%m%d')}"

    async with superuser_engine.begin() as conn:
        await conn.execute(text("SELECT create_events_partition(:d)"), {"d": past_day})
        await conn.execute(text("SELECT create_events_partition(:d)"), {"d": future_day})

    try:
        await enforce_retention({})

        async with superuser_engine.connect() as conn:
            past_gone = (
                await conn.execute(
                    text(f"SELECT to_regclass('public.{past_name}') IS NULL")
                )
            ).scalar_one()
            future_survives = (
                await conn.execute(
                    text(f"SELECT to_regclass('public.{future_name}') IS NOT NULL")
                )
            ).scalar_one()
            assert past_gone is True, "partition far older than any retention cutoff should drop"
            assert future_survives is True, "partition far in the future should never be dropped"
    finally:
        # enforce_retention already removed the past partition; only the
        # future one needs explicit cleanup so it doesn't linger.
        async with superuser_engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {future_name}"))


@pytest.mark.db
async def test_enforce_retention_uses_fallback_when_no_projects_exist(superuser_engine) -> None:
    async with superuser_engine.connect() as conn:
        org_count = (await conn.execute(text("SELECT count(*) FROM orgs"))).scalar_one()
    if org_count != 0:
        pytest.skip(
            "ambient orgs present in this database; cannot safely prove the "
            "empty-projects fallback path without a clean-slate orgs table"
        )

    projects_by_org = await _load_projects_by_org()
    assert projects_by_org == {}
    assert fold_global_max_retention_days([]) == DEFAULT_RETENTION_DAYS_FALLBACK


@pytest.mark.db
async def test_per_project_trim_deletes_only_that_projects_over_retention_rows(
    superuser_engine, app_sessionmaker, retention_two_orgs
) -> None:
    """Prove the per-project trim step is bounded, isolated per org, and RLS-respected.

    Both events below are 3 days old: older than project_a's retention_days=1
    (must be trimmed) but younger than project_b's retention_days=30 (must
    survive) -- and younger than any realistic global partition-floor cutoff,
    so this proves the TRIM mechanism specifically, not the partition drop.
    """
    org_a = retention_two_orgs["org_a"]
    org_b = retention_two_orgs["org_b"]
    project_a = retention_two_orgs["project_a"]
    project_b = retention_two_orgs["project_b"]

    day = datetime.date.today() - datetime.timedelta(days=3)
    partition_name = f"events_{day.strftime('%Y%m%d')}"
    event_a, event_b = uuid.uuid4(), uuid.uuid4()

    async with superuser_engine.begin() as conn:
        await conn.execute(text("SELECT create_events_partition(:d)"), {"d": day})
        for org, project, event_id in ((org_a, project_a, event_a), (org_b, project_b, event_b)):
            await conn.execute(
                text(
                    "INSERT INTO events "
                    "(org_id, project_id, event_id, received_at, environment, level, payload) "
                    "VALUES (:org, :proj, :eid, now() - interval '3 days', "
                    "'production', 'error', '{}'::jsonb)"
                ),
                {"org": org, "proj": project, "eid": event_id},
            )

    try:
        # Exercise the REAL production code path end to end: the same
        # cross-org project load enforce_retention uses, through the
        # non-superuser, RLS-bound role, then the same trim step
        # enforce_retention calls.
        projects_by_org = await _load_projects_by_org(session_factory=app_sessionmaker)
        assert projects_by_org.get(org_a) == [(project_a, 1)]
        assert projects_by_org.get(org_b) == [(project_b, 30)]

        all_retention_days = [
            retention_days
            for projects in projects_by_org.values()
            for (_pid, retention_days) in projects
        ]
        global_max = fold_global_max_retention_days(all_retention_days)
        # project_b's own retention_days (30) is always one of the folded
        # values, so global_max >= 30 regardless of any ambient orgs.
        assert global_max >= 30

        await _trim_over_retention_projects(
            projects_by_org, global_max, session_factory=app_sessionmaker
        )

        # project_a's event (retention_days=1, event is 3 days old): trimmed.
        async with tenant_session(str(org_a), session_factory=app_sessionmaker) as session:
            remaining_a = (
                await session.execute(
                    text("SELECT count(*) FROM events WHERE event_id = :eid"), {"eid": event_a}
                )
            ).scalar_one()
            assert remaining_a == 0

        # project_b's event (retention_days=30, event is 3 days old): survives.
        async with tenant_session(str(org_b), session_factory=app_sessionmaker) as session:
            remaining_b = (
                await session.execute(
                    text("SELECT count(*) FROM events WHERE event_id = :eid"), {"eid": event_b}
                )
            ).scalar_one()
            assert remaining_b == 1

        # RLS-respected: org_a's scope never sees org_b's surviving event.
        async with tenant_session(str(org_a), session_factory=app_sessionmaker) as session:
            leaked = (
                await session.execute(
                    text("SELECT count(*) FROM events WHERE event_id = :eid"), {"eid": event_b}
                )
            ).scalar_one()
            assert leaked == 0
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {partition_name}"))


@pytest.mark.db
async def test_partition_functions_executable_by_nonsuperuser_app_role(
    superuser_engine, app_sessionmaker
) -> None:
    """Prove migration 0003 closed the SECURITY INVOKER privilege gap.

    Exactly the failure W2-04 flagged: a non-superuser member of
    ``crashlens_app`` (the ``crashlens_test`` login role) calling the
    partition functions used to hit a "must be owner of relation events"
    privilege error, because SECURITY INVOKER DDL required ownership of the
    parent table. After 0003 (SECURITY DEFINER, pinned search_path, EXECUTE
    narrowed to crashlens_app) both calls must succeed without any privilege
    error.
    """
    # Ground the migration's effect first: both functions are SECURITY
    # DEFINER with a pinned search_path.
    async with superuser_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT proname, prosecdef, proconfig FROM pg_proc "
                    "WHERE proname IN "
                    "('create_events_partition', 'drop_events_partitions_before')"
                )
            )
        ).all()
        assert {row.proname for row in rows} == {
            "create_events_partition",
            "drop_events_partitions_before",
        }
        for row in rows:
            assert row.prosecdef is True, f"{row.proname} is not SECURITY DEFINER"
            assert row.proconfig is not None and any(
                setting.startswith("search_path=") for setting in row.proconfig
            ), f"{row.proname} has no pinned search_path"

    future_day = datetime.date(2998, 3, 1)
    future_name = f"events_{future_day.strftime('%Y%m%d')}"
    try:
        # create_events_partition for a future day, as the non-superuser
        # app-role member. Would have raised a privilege error before 0003.
        async with app_sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT create_events_partition(:d)"), {"d": future_day}
                )
        async with superuser_engine.connect() as conn:
            created = (
                await conn.execute(
                    text(f"SELECT to_regclass('public.{future_name}') IS NOT NULL")
                )
            ).scalar_one()
            assert created is True

        # drop_events_partitions_before with an ANCIENT cutoff, same role: no
        # privilege error, and (nothing predates 1900) no partition removed --
        # including the future one just created.
        async with app_sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT drop_events_partitions_before(DATE '1900-01-01')")
                )
        async with superuser_engine.connect() as conn:
            survives = (
                await conn.execute(
                    text(f"SELECT to_regclass('public.{future_name}') IS NOT NULL")
                )
            ).scalar_one()
            assert survives is True
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {future_name}"))
