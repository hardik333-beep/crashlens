"""Integration tests for the v1 schema and RLS tenant isolation.

Marked ``db``: they require a live PostgreSQL with the migration applied. When no
database is reachable they SKIP cleanly (so ``pytest -q`` passes locally without
Postgres); in CI the ``pytest`` job starts a postgres:16 service, runs
``alembic upgrade head``, and these execute for real.

RLS is only enforced for non-superuser roles, so these tests connect as a
dedicated non-superuser login role ``crashlens_test`` that is a member of BOTH
privilege roles created by the migration: ``crashlens_app`` (FORCE-RLS-bound
DML) and ``crashlens_system`` (read-only BYPASSRLS bootstrap, entered per
transaction via SET LOCAL ROLE). Seed data is written through the superuser
connection, which bypasses RLS by design.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db import system_session, tenant_session

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
        pytest.skip("PostgreSQL not reachable; skipping db integration tests")
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
        # Membership in the bootstrap role authorizes SET LOCAL ROLE
        # crashlens_system inside system_session.
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
    """Seed two orgs (A and B), each with a project, DSN key, and membership.

    One shared user is a member of BOTH orgs (the login bootstrap case). Written
    via the superuser connection (RLS-bypassing) so setup does not depend on the
    very isolation we are testing. Rows are removed on teardown; deleting the
    orgs cascades projects, dsn_keys, and memberships.
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    proj_a, proj_b = uuid.uuid4(), uuid.uuid4()
    key_a, key_b = uuid.uuid4(), uuid.uuid4()
    user_id = uuid.uuid4()
    dsn_a, dsn_b = f"pk-a-{key_a}", f"pk-b-{key_b}"
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash) "
                "VALUES (:id, :email, 'x')"
            ),
            {"id": user_id, "email": f"seed-{user_id}@example.test"},
        )
        for org, slug in ((org_a, "org-a"), (org_b, "org-b")):
            await conn.execute(
                text("INSERT INTO orgs (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": org, "name": slug, "slug": f"{slug}-{org}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO org_memberships (org_id, user_id, role) "
                    "VALUES (:org, :user, 'member')"
                ),
                {"org": org, "user": user_id},
            )
        for proj, org, slug in ((proj_a, org_a, "proj-a"), (proj_b, org_b, "proj-b")):
            await conn.execute(
                text(
                    "INSERT INTO projects (id, org_id, name, slug) "
                    "VALUES (:id, :org, :name, :slug)"
                ),
                {"id": proj, "org": org, "name": slug, "slug": slug},
            )
        for key, org, proj, pk in ((key_a, org_a, proj_a, dsn_a), (key_b, org_b, proj_b, dsn_b)):
            await conn.execute(
                text(
                    "INSERT INTO dsn_keys (id, org_id, project_id, public_key) "
                    "VALUES (:id, :org, :proj, :pk)"
                ),
                {"id": key, "org": org, "proj": proj, "pk": pk},
            )
    yield {
        "org_a": org_a,
        "org_b": org_b,
        "proj_a": proj_a,
        "proj_b": proj_b,
        "dsn_a": dsn_a,
        "dsn_b": dsn_b,
        "user_id": user_id,
    }
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM orgs WHERE id IN (:a, :b)"), {"a": org_a, "b": org_b}
        )
        await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


async def test_schema_and_policies_exist(superuser_engine) -> None:
    async with superuser_engine.connect() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            )
        ).scalars().all()
        for expected in (
            "users",
            "orgs",
            "org_memberships",
            "org_invites",
            "projects",
            "dsn_keys",
            "releases",
            "issues",
            "events",
            "issue_comments",
            "alert_channels",
            "audit_log",
        ):
            assert expected in tables, f"missing table {expected}"

        # events is RANGE partitioned.
        partitioned = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_partitioned_table pt "
                    "JOIN pg_class c ON c.oid = pt.partrelid WHERE c.relname = 'events'"
                )
            )
        ).scalar_one()
        assert partitioned == 1

        # One tenant_isolation policy per tenant table (orgs + 10 org-scoped).
        policies = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE policyname = 'tenant_isolation'"
                )
            )
        ).scalar_one()
        assert policies == 11

        # RLS is forced on projects (representative tenant table).
        forced = (
            await conn.execute(
                text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'projects'")
            )
        ).scalar_one()
        assert forced is True

        # The application role exists and is not a superuser.
        is_super = (
            await conn.execute(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = 'crashlens_app'")
            )
        ).scalar_one()
        assert is_super is False

        # The bootstrap role exists, is not a superuser, and carries BYPASSRLS.
        row = (
            await conn.execute(
                text(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = 'crashlens_system'"
                )
            )
        ).one()
        assert row.rolsuper is False
        assert row.rolbypassrls is True


@pytest.mark.isolation
async def test_rls_denies_without_org_context(app_sessionmaker, two_orgs) -> None:
    # A plain session (no GUC, no SET ROLE) sees nothing: current_setting
    # returns NULL and the org predicate fails, so RLS denies reads.
    async with app_sessionmaker() as session:
        visible = (
            await session.execute(text("SELECT count(*) FROM projects"))
        ).scalar_one()
        assert visible == 0

    # And it cannot insert: WITH CHECK fails with no org context.
    with pytest.raises(DBAPIError):
        async with app_sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO projects (org_id, name, slug) "
                        "VALUES (:org, 'x', 'x')"
                    ),
                    {"org": two_orgs["org_a"]},
                )


@pytest.mark.isolation
async def test_cross_org_reads_writes_updates_denied(app_sessionmaker, two_orgs) -> None:
    org_a = two_orgs["org_a"]
    org_b = two_orgs["org_b"]
    proj_a = two_orgs["proj_a"]
    proj_b = two_orgs["proj_b"]

    # Scoped to A: sees only A's project, never B's.
    async with tenant_session(str(org_a), session_factory=app_sessionmaker) as session:
        ids = (
            await session.execute(text("SELECT id FROM projects"))
        ).scalars().all()
        assert proj_a in ids
        assert proj_b not in ids

    # INSERT of a B-owned row from A's scope is rejected by WITH CHECK. The
    # error is isolated to its own transaction so it rolls back cleanly.
    with pytest.raises(DBAPIError):
        async with tenant_session(
            str(org_a), session_factory=app_sessionmaker
        ) as session:
            await session.execute(
                text(
                    "INSERT INTO projects (org_id, name, slug) "
                    "VALUES (:org, 'sneaky', 'sneaky')"
                ),
                {"org": org_b},
            )

    # UPDATE of B's row from A's scope affects zero rows (B's row is invisible).
    async with tenant_session(str(org_a), session_factory=app_sessionmaker) as session:
        result = await session.execute(
            text("UPDATE projects SET name = 'hacked' WHERE id = :id"),
            {"id": proj_b},
        )
        assert result.rowcount == 0

    # DELETE of B's row from A's scope affects zero rows.
    async with tenant_session(str(org_a), session_factory=app_sessionmaker) as session:
        result = await session.execute(
            text("DELETE FROM projects WHERE id = :id"), {"id": proj_b}
        )
        assert result.rowcount == 0

    # B's project survived untouched (checked via the RLS-bypassing superuser
    # through B's own scope).
    async with tenant_session(str(org_b), session_factory=app_sessionmaker) as session:
        name = (
            await session.execute(
                text("SELECT name FROM projects WHERE id = :id"), {"id": proj_b}
            )
        ).scalar_one()
        assert name == "proj-b"


@pytest.mark.isolation
async def test_tenant_session_scopes_reads(app_sessionmaker, two_orgs) -> None:
    async with tenant_session(
        str(two_orgs["org_a"]), session_factory=app_sessionmaker
    ) as session:
        count_a = (
            await session.execute(text("SELECT count(*) FROM projects"))
        ).scalar_one()
    async with tenant_session(
        str(two_orgs["org_b"]), session_factory=app_sessionmaker
    ) as session:
        count_b = (
            await session.execute(text("SELECT count(*) FROM projects"))
        ).scalar_one()
    assert count_a == 1
    assert count_b == 1


@pytest.mark.isolation
async def test_events_rls_isolation(app_sessionmaker, two_orgs) -> None:
    # Insert an event owned by A (today's partition exists from the migration).
    event_id = uuid.uuid4()
    async with tenant_session(
        str(two_orgs["org_a"]), session_factory=app_sessionmaker
    ) as session:
        await session.execute(
            text(
                "INSERT INTO events "
                "(org_id, project_id, event_id, environment, level, payload) "
                "VALUES (:org, :proj, :eid, 'production', 'error', '{}'::jsonb)"
            ),
            {"org": two_orgs["org_a"], "proj": two_orgs["proj_a"], "eid": event_id},
        )

    # B cannot see A's event.
    async with tenant_session(
        str(two_orgs["org_b"]), session_factory=app_sessionmaker
    ) as session:
        seen_by_b = (
            await session.execute(
                text("SELECT count(*) FROM events WHERE event_id = :eid"),
                {"eid": event_id},
            )
        ).scalar_one()
        assert seen_by_b == 0

    # A can.
    async with tenant_session(
        str(two_orgs["org_a"]), session_factory=app_sessionmaker
    ) as session:
        seen_by_a = (
            await session.execute(
                text("SELECT count(*) FROM events WHERE event_id = :eid"),
                {"eid": event_id},
            )
        ).scalar_one()
        assert seen_by_a == 1


async def test_system_session_reads_bootstrap_tables_across_orgs(
    app_sessionmaker, two_orgs
) -> None:
    # (a) With NO GUC set, system_session (SET LOCAL ROLE crashlens_system,
    # BYPASSRLS) can read dsn_keys and org_memberships rows belonging to TWO
    # different orgs: the four bootstrap flows work before org context exists.
    async with system_session(session_factory=app_sessionmaker) as session:
        key_orgs = (
            await session.execute(
                text(
                    "SELECT org_id FROM dsn_keys WHERE public_key IN (:a, :b)"
                ),
                {"a": two_orgs["dsn_a"], "b": two_orgs["dsn_b"]},
            )
        ).scalars().all()
        assert set(key_orgs) == {two_orgs["org_a"], two_orgs["org_b"]}

        member_orgs = (
            await session.execute(
                text("SELECT org_id FROM org_memberships WHERE user_id = :uid"),
                {"uid": two_orgs["user_id"]},
            )
        ).scalars().all()
        assert set(member_orgs) == {two_orgs["org_a"], two_orgs["org_b"]}


async def test_system_session_cannot_write(app_sessionmaker, two_orgs) -> None:
    # (b) The bootstrap role is SELECT-only: INSERT raises a privilege error.
    with pytest.raises(DBAPIError):
        async with system_session(session_factory=app_sessionmaker) as session:
            await session.execute(
                text(
                    "INSERT INTO dsn_keys (org_id, project_id, public_key) "
                    "VALUES (:org, :proj, 'pk-forbidden')"
                ),
                {"org": two_orgs["org_a"], "proj": two_orgs["proj_a"]},
            )


async def test_system_session_cannot_read_outside_the_four_tables(
    app_sessionmaker, two_orgs
) -> None:
    # (c) crashlens_system has no SELECT grant on issues (or any table beyond
    # the four): the read raises a privilege error instead of bypassing RLS.
    with pytest.raises(DBAPIError):
        async with system_session(session_factory=app_sessionmaker) as session:
            await session.execute(text("SELECT count(*) FROM issues"))


@pytest.mark.isolation
async def test_plain_session_does_not_inherit_bypass(app_sessionmaker, two_orgs) -> None:
    # (d) The bypass is opt-in per transaction: a plain app session (no SET
    # ROLE, no GUC) is still RLS-bound and reads zero rows from dsn_keys even
    # though seeded rows exist.
    async with app_sessionmaker() as session:
        visible = (
            await session.execute(text("SELECT count(*) FROM dsn_keys"))
        ).scalar_one()
        assert visible == 0


async def test_partition_functions_create_and_drop(superuser_engine) -> None:
    # Use a far-future date to avoid colliding with the migration's partitions.
    async with superuser_engine.begin() as conn:
        await conn.execute(text("SELECT create_events_partition(DATE '2999-01-01')"))
        exists = (
            await conn.execute(
                text("SELECT to_regclass('public.events_29990101') IS NOT NULL")
            )
        ).scalar_one()
        assert exists is True

        await conn.execute(
            text("SELECT drop_events_partitions_before(DATE '2999-01-02')")
        )
        gone = (
            await conn.execute(
                text("SELECT to_regclass('public.events_29990101') IS NULL")
            )
        ).scalar_one()
        assert gone is True
