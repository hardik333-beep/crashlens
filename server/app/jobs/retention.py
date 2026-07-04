"""Partition maintenance and retention background jobs.

Two arq-schedulable jobs (registered as cron jobs in ``app/worker.py``):

``maintain_event_partitions``
    Pre-creates the daily ``events`` partitions for today through today + 7,
    the same lookahead migration 0001 seeds at deploy time, so ingest always
    has a home for ``received_at`` without an operator having to intervene.

``enforce_retention``
    Reclaims space for expired events via TWO mechanisms, in this order:

    (a) PARTITION DROP (primary). ``DROP TABLE`` on a whole daily partition is
        an instant catalog operation: no row scan, no WAL per deleted row, no
        dead tuples left behind for autovacuum to reclaim. A mass
        ``DELETE FROM events WHERE received_at < ...`` over millions of rows
        does the opposite: every deleted row becomes an MVCC dead tuple, VACUUM
        has to walk and reclaim all of them, and on the small single-node
        VPSes Crashlens is designed to self-host on, that vacuum pass can pin
        a CPU core and bloat the table/index files for hours. Partition drop
        is therefore the primary mechanism and mass DELETE is never used
        against the full table; this module never issues an unbounded DELETE.

    (b) PER-PROJECT TRIM (secondary, bounded). Partition drop can only honor
        ONE global cutoff (the partition boundaries are daily and shared by
        every project). A project configured for a SHORTER retention than the
        global maximum still has its expired rows sitting in partitions that
        are not yet old enough to drop. Those rows are removed with a
        ``DELETE ... WHERE project_id = :p AND received_at < ...``, but the
        delete is bounded to just the gap between that project's own
        retention and the global partition floor, never the whole table, so
        the MVCC bloat concern above does not apply here at any meaningful
        scale.

RLS-CONTEXT-BEFORE-LOAD (worker rule)
--------------------------------------
Every org-scoped read or write in this module opens a fresh
``tenant_session(org_id)`` (see ``app/db.py``) and the RLS scope is applied as
that transaction's FIRST statement before any row is touched, never after.
This is a hard project gate for background workers specifically: a worker
loop that reads rows first and applies tenant scope afterward (e.g. to decide
how to process what it already fetched) can observe cross-tenant data before
the scope ever takes effect. ``tenant_session`` structurally prevents that by
applying the GUC as the first statement of the transaction it opens, so there
is no window in which unscoped rows are visible to this module's code.

WHY PARTITION DDL RUNS ON A PLAIN SESSION, NOT tenant_session/system_session
-----------------------------------------------------------------------------
``create_events_partition`` and ``drop_events_partitions_before`` (both
defined in migration 0001) operate on ``events`` as a whole -- they attach or
detach a physical child table of the partitioned parent. That is DDL against
the catalog, not a row-level SELECT/INSERT/UPDATE/DELETE, so PostgreSQL Row
Level Security (which only ever filters rows returned by DML) has nothing to
apply here regardless of which role or GUC is active; there is no "org" a
partition boundary belongs to. Calling these functions is therefore done on a
plain session from the default sessionmaker (no ``SET LOCAL ROLE``, no
``app.current_org`` GUC) rather than through ``tenant_session`` or
``system_session``, both of which exist specifically to manage ROW visibility
that does not apply to this operation.

PRIVILEGE MODEL FOR THE PARTITION FUNCTIONS (resolved; governor 2026-07-04)
-----------------------------------------------------------------------------
As originally created by migration 0001, both functions were the PostgreSQL
default ``SECURITY INVOKER``, but attaching/detaching a partition
(``CREATE TABLE ... PARTITION OF`` / ``DROP TABLE`` on a partition) requires
the invoking role to OWN the parent ``events`` table -- and the worker's
deployed login role is a non-superuser member of ``crashlens_app`` with only
``SELECT, INSERT, UPDATE, DELETE``, not ownership, so both jobs' DDL calls
would have failed in production with a "must be owner of relation events"
privilege error. This slice flagged the gap instead of widening grants; the
governor decided the fix on 2026-07-04 and migration 0003
(``0003_partition_fn_secdef``) implements it:

- Both functions are now ``SECURITY DEFINER``: the DDL inside runs with the
  function OWNER's privileges (the migration/schema-owner role that owns
  ``events``), so the caller no longer needs table ownership.
- Their ``search_path`` is pinned (``SET search_path = public, pg_temp``),
  mandatory on any SECURITY DEFINER function: an unpinned definer resolves
  names via the CALLER's search_path, letting a malicious caller shadow
  public objects and escalate via the definer's privileges.
- ``EXECUTE`` is revoked from PUBLIC and granted only to ``crashlens_app``,
  so SECURITY DEFINER does not turn Postgres's default PUBLIC-execute grant
  into "any connected role may run partition DDL as the owner".

Net effect for this module: the worker's login role (member of
``crashlens_app``) can call both functions on a plain session, and nothing
else changes here.

CROSS-ORG READ FOR retention_days: WHY IT GOES THROUGH tenant_session PER ORG
-------------------------------------------------------------------------------
Computing the global retention ceiling requires ``MAX(projects.retention_days)``
across every org, but ``crashlens_system`` (the read-only BYPASSRLS bootstrap
role) is granted SELECT on exactly ``orgs``, ``org_memberships``,
``org_invites``, and ``dsn_keys`` (see ``app/db.py`` and migration 0001) --
NOT ``projects``. There is no role in this system authorized to read
``projects`` across every org in one query. So this module reads the one
cross-tenant table it IS authorized to sweep (``orgs``, via
``system_session``) to get the org id list, then opens one
``tenant_session(org_id)`` per org to read that org's own ``projects`` rows,
exactly as any other tenant-scoped read would.
"""

import datetime
import logging
import re
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import sourcemaps
from app.config import get_settings
from app.db import events_partition_name, get_sessionmaker, system_session, tenant_session

logger = logging.getLogger(__name__)

# FLAGGED DEFAULT: retention ceiling used only when there are no projects at
# all (fresh install, or every project deleted). Chosen to match the
# migration's own default `projects.retention_days` of 30 with headroom;
# review before relying on it in production.
DEFAULT_RETENTION_DAYS_FALLBACK = 90

# Matches events_YYYYMMDD, the naming convention shared by migration 0001's
# SQL functions and app.db.events_partition_name.
_PARTITION_NAME_RE = re.compile(r"^events_(\d{8})$")


def lookahead_partition_days(
    today: datetime.date, days_ahead: int = 7
) -> list[datetime.date]:
    """Return ``today`` through ``today + days_ahead`` inclusive.

    Pure function (no I/O): mirrors the exact lookahead migration 0001 seeds
    at deploy time (``range(8)`` -> today..today+7), so
    :func:`maintain_event_partitions` keeps that same window going forward.
    """
    return [today + datetime.timedelta(days=offset) for offset in range(days_ahead + 1)]


def compute_retention_cutoff(
    today: datetime.date, global_max_retention_days: int
) -> datetime.date:
    """Return the partition-drop cutoff date: ``today - global_max_retention_days``.

    Pure function. Any partition dated strictly before this cutoff is not
    required by ANY project's retention policy and is safe to drop.
    """
    return today - datetime.timedelta(days=global_max_retention_days)


def fold_global_max_retention_days(
    retention_days_values: list[int], fallback: int = DEFAULT_RETENTION_DAYS_FALLBACK
) -> int:
    """Return ``max(retention_days_values)``, or ``fallback`` if the list is empty.

    Pure function. The empty case is "no projects exist anywhere" (fresh
    install or every project deleted): with nothing to preserve, retention
    falls back to a fixed default rather than dropping every partition.
    """
    if not retention_days_values:
        return fallback
    return max(retention_days_values)


def partitions_older_than(
    partition_names: list[str], cutoff: datetime.date
) -> list[str]:
    """Return the subset of ``partition_names`` (``events_YYYYMMDD``) dated before ``cutoff``.

    Pure function used only to produce an honest, specific INFO log of which
    partitions a drop call is about to remove; the actual authoritative
    decision of what to drop is made by the SQL function
    ``drop_events_partitions_before`` itself, matching logic. Names that do
    not match the naming convention are ignored (defensive: this module never
    assumes every child of ``events`` is one of its own daily partitions).
    """
    older = []
    for name in partition_names:
        match = _PARTITION_NAME_RE.match(name)
        if not match:
            continue
        day = datetime.datetime.strptime(match.group(1), "%Y%m%d").date()
        if day < cutoff:
            older.append(name)
    return older


def projects_needing_trim(
    projects: list[tuple[uuid.UUID, int]], global_max_retention_days: int
) -> list[tuple[uuid.UUID, int]]:
    """Return the ``(project_id, retention_days)`` pairs below the global ceiling.

    Pure function. These are the only projects the per-project trim touches:
    anything at or above the global ceiling is already fully covered by the
    partition drop.
    """
    return [
        (project_id, retention_days)
        for project_id, retention_days in projects
        if retention_days < global_max_retention_days
    ]


async def maintain_event_partitions(
    ctx: dict,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Ensure the ``events`` partitions for today through today + 7 exist.

    Idempotent: ``create_events_partition`` is ``CREATE TABLE IF NOT EXISTS``
    under the hood, so re-running this (daily, or manually) is always safe,
    including concurrently with ingest writing to today's partition -- this
    job only ever creates FUTURE partitions or re-confirms existing ones, it
    never touches a partition ingest is actively writing to in a way that
    could conflict.

    Runs on a plain session (see module docstring: "WHY PARTITION DDL RUNS ON
    A PLAIN SESSION"): partition attach is DDL, not row-level DML, so RLS does
    not apply here and neither ``tenant_session`` nor ``system_session`` is
    the right tool.

    ``session_factory`` is injectable for tests (see ``app/db.py``'s same
    pattern on ``tenant_session``/``system_session``); arq calls this with
    only ``ctx``, so production always uses the default factory.
    """
    today = datetime.date.today()
    days = lookahead_partition_days(today)
    factory = session_factory or get_sessionmaker()
    touched: list[str] = []
    async with factory() as session:
        async with session.begin():
            for day in days:
                await session.execute(
                    text("SELECT create_events_partition(:day)"), {"day": day}
                )
                touched.append(events_partition_name(day))
    logger.info(
        "maintain_event_partitions: ensured partitions exist count=%d names=%s",
        len(touched),
        touched,
    )


async def _all_org_ids(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> list[uuid.UUID]:
    """Return every org id via ``system_session`` (the one cross-tenant read it is granted)."""
    async with system_session(session_factory=session_factory) as session:
        return list((await session.execute(text("SELECT id FROM orgs"))).scalars().all())


async def _load_projects_by_org(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[uuid.UUID, list[tuple[uuid.UUID, int]]]:
    """Return ``{org_id: [(project_id, retention_days), ...]}`` for every org.

    See module docstring "CROSS-ORG READ FOR retention_days": ``projects`` has
    no cross-tenant grant, so this reads the org list once via
    ``system_session`` and then opens one ``tenant_session(org_id)`` per org,
    the RLS scope applied before that org's rows are ever loaded.
    """
    result: dict[uuid.UUID, list[tuple[uuid.UUID, int]]] = {}
    for org_id in await _all_org_ids(session_factory=session_factory):
        async with tenant_session(str(org_id), session_factory=session_factory) as session:
            rows = (
                await session.execute(text("SELECT id, retention_days FROM projects"))
            ).all()
        result[org_id] = [(row[0], row[1]) for row in rows]
    return result


async def _existing_partition_names(session: AsyncSession) -> list[str]:
    """Return the child partition table names currently attached to ``events``."""
    return list(
        (
            await session.execute(
                text(
                    "SELECT c.relname FROM pg_inherits i "
                    "JOIN pg_class c ON c.oid = i.inhrelid "
                    "JOIN pg_class p ON p.oid = i.inhparent "
                    "WHERE p.relname = 'events'"
                )
            )
        )
        .scalars()
        .all()
    )


async def enforce_retention(
    ctx: dict,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Reclaim expired events: partition drop first, then bounded per-project trim.

    See the module docstring for the full never-mass-DELETE rationale and the
    RLS-context-before-load rule this function follows for every per-org
    read/write. Both mechanisms are idempotent and safe to run concurrently
    with ingest: partition drop only ever removes partitions already past
    every project's retention window (never today's), and the per-project
    trim is a normal RLS-scoped DELETE, no different from any other tenant
    write path.

    ``session_factory`` is injectable for tests (same pattern as
    ``tenant_session``/``system_session`` in ``app/db.py``); arq calls this
    with only ``ctx``, so production always uses the default factory.
    """
    projects_by_org = await _load_projects_by_org(session_factory=session_factory)
    all_retention_days = [
        retention_days
        for projects in projects_by_org.values()
        for (_project_id, retention_days) in projects
    ]
    global_max = fold_global_max_retention_days(all_retention_days)
    if not all_retention_days:
        logger.info(
            "enforce_retention: no projects found anywhere; using fallback "
            "global_max_retention_days=%d",
            global_max,
        )

    today = datetime.date.today()
    cutoff = compute_retention_cutoff(today, global_max)

    # (a) PARTITION DROP -- the primary mechanism (see module docstring). Plain
    # session: dropping a partition is DDL, not row-level DML, so RLS does
    # not apply (same rationale as maintain_event_partitions).
    factory = session_factory or get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            existing = await _existing_partition_names(session)
            about_to_drop = partitions_older_than(existing, cutoff)
            await session.execute(
                text("SELECT drop_events_partitions_before(:cutoff)"), {"cutoff": cutoff}
            )
    logger.info(
        "enforce_retention: dropped partitions cutoff=%s global_max_retention_days=%d "
        "count=%d names=%s",
        cutoff,
        global_max,
        len(about_to_drop),
        about_to_drop,
    )

    # (b) PER-PROJECT TRIM -- secondary, bounded (see module docstring).
    await _trim_over_retention_projects(
        projects_by_org, global_max, session_factory=session_factory
    )

    # (c) SOURCE MAP PRUNE (W6-01). Uploaded source map release directories are
    # files on disk, not DB rows, so they are reclaimed here on the SAME global
    # cutoff: any release directory whose mtime predates the partition-drop
    # cutoff is older than every project's retention window and safe to remove.
    # Same logging discipline as the steps above; a per-directory error is
    # logged and skipped, never fatal.
    cutoff_dt = datetime.datetime.combine(
        cutoff, datetime.time.min, tzinfo=datetime.UTC
    )
    removed_maps = sourcemaps.prune_expired_release_maps(
        get_settings().sourcemaps_dir, cutoff_dt
    )
    logger.info(
        "enforce_retention: pruned source map release dirs cutoff=%s count=%d",
        cutoff,
        len(removed_maps),
    )


async def _trim_over_retention_projects(
    projects_by_org: dict[uuid.UUID, list[tuple[uuid.UUID, int]]],
    global_max_retention_days: int,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """(b) PER-PROJECT TRIM: bounded, RLS-scoped delete for under-ceiling projects.

    Split out of :func:`enforce_retention` so it can be exercised on its own:
    unlike the partition-drop step, this is a normal DML DELETE (no table
    ownership required), so it is the piece integration tests can run against
    a genuine non-superuser, RLS-bound role to prove isolation for real. Each
    org's rows are only ever touched inside that org's own ``tenant_session``,
    RLS scope applied before any row is loaded (see module docstring
    "RLS-CONTEXT-BEFORE-LOAD").
    """
    for org_id, projects in projects_by_org.items():
        under_retention = projects_needing_trim(projects, global_max_retention_days)
        if not under_retention:
            continue
        async with tenant_session(str(org_id), session_factory=session_factory) as session:
            for project_id, retention_days in under_retention:
                result = await session.execute(
                    text(
                        "DELETE FROM events WHERE project_id = :project_id "
                        "AND received_at < now() - make_interval(days => :retention_days)"
                    ),
                    {"project_id": project_id, "retention_days": retention_days},
                )
                logger.info(
                    "enforce_retention: trimmed org=%s project=%s retention_days=%d "
                    "rows_deleted=%d",
                    org_id,
                    project_id,
                    retention_days,
                    result.rowcount,
                )
