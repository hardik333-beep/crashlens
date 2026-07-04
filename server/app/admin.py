"""Instance-admin panel services: the self-hoster's operator view.

The instance-admin panel is the OPERATOR surface (distinct from the per-org
tenant surface): whole-instance counts, recent event volume, queue depth,
event-partition stats, per-org rollups, and the user list with the
instance-admin toggle. Every function here is called only from
``app/routes/admin.py`` behind the ``require_instance_admin`` dependency.

SESSION CHOICE PER OPERATION (grounded in app/db.py):

* All cross-tenant READS (counts, per-org rollups, event volume, partitions)
  go through :func:`app.db.admin_session` -- the read-only, BYPASSRLS
  ``crashlens_admin`` role (migration 0007), SELECT-only on exactly the tables
  read here. It can see every tenant's rows but cannot write anything.
* The ONE write in the whole panel -- toggling ``users.is_instance_admin`` --
  runs on a plain ``crashlens_app`` session against the RLS-exempt ``users``
  table, NOT through ``crashlens_admin`` (which has no write grant). The
  last-admin guard runs in that same transaction.
* db / redis reachability reuse the existing health probes; queue depth reads
  the arq queue key directly from a short-lived Redis client.

SECRETS HYGIENE: no secret is read or logged here. Alert-channel CONFIG (which
can embed webhook URLs/tokens) is deliberately never selected -- the per-org
rollup counts channels, it does not read their config.
"""

import asyncio
import datetime
import logging

import redis.asyncio as redis
from arq.constants import default_queue_name
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import admin_session, get_sessionmaker
from app.health import check_database, check_redis

logger = logging.getLogger(__name__)

# Pagination defaults/clamps for the orgs and users lists (mirrors the audit
# slice's clamp discipline so a client cannot request an unbounded page).
DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 100

# Recent-volume window for the overview's events count.
_EVENTS_WINDOW = datetime.timedelta(hours=24)

# The arq queue is a Redis SORTED SET (arq enqueues with ZADD, score = run-at
# timestamp; see arq.connections.enqueue_job), so its depth is ZCARD, not LLEN.
# The key name is arq's own constant, not guessed.
_QUEUE_KEY = default_queue_name

# Matches the events_YYYYMMDD daily partitions (shared with migration 0001 and
# app.db.events_partition_name); used to list only Crashlens' own partitions.
_PARTITION_NAME_PATTERN = "^events_[0-9]{8}$"

_PROBE_TIMEOUT_SECONDS = 2.0


def clamp_page(page: int | None) -> int:
    """Return a 1-based page number, floored at 1."""
    if page is None or page < 1:
        return 1
    return page


def clamp_per_page(per_page: int | None) -> int:
    """Return a per-page size within ``[1, MAX_PER_PAGE]`` (default when unset)."""
    if per_page is None or per_page < 1:
        return DEFAULT_PER_PAGE
    return min(per_page, MAX_PER_PAGE)


def would_orphan_instance(*, enabled: bool, is_self: bool, admin_count: int) -> bool:
    """Return True if this toggle would leave the instance with zero admins.

    Pure logic (unit-tested without a database). The panel forbids an
    instance admin from removing THEIR OWN flag when they are the last one
    standing: ``enabled`` is the requested new value, ``is_self`` whether the
    target is the caller, and ``admin_count`` the current number of instance
    admins. Removing another admin is always allowed (the caller remains), and
    turning a flag ON never orphans anything.
    """
    return (not enabled) and is_self and admin_count <= 1


async def _queue_depth(redis_url: str) -> int | None:
    """Return the number of jobs waiting in the arq queue, or None if unreachable.

    The arq queue is a sorted set, so depth is ZCARD of the queue key. A brief,
    dedicated client is used (the panel is not on any hot path); any failure
    yields None so the overview still renders when Redis is down.
    """
    client = redis.from_url(redis_url)
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            return int(await client.zcard(_QUEUE_KEY))
    except Exception:
        logger.warning("admin overview: queue depth probe failed")
        return None
    finally:
        await client.aclose()


async def _read_counts_and_partitions(
    session: AsyncSession,
) -> dict:
    """Read whole-instance counts, 24h event volume, and partition stats.

    Runs inside an :func:`admin_session` transaction (BYPASSRLS), so the counts
    span every tenant. ``pg_class.reltuples`` is the planner's row ESTIMATE (an
    exact ``count(*)`` on the events partitions could scan millions of rows on a
    self-hosted box); it is -1 before a table's first ANALYZE, so it is floored
    at 0. The counts on the small metadata tables are exact.
    """
    users_count = (await session.execute(text("SELECT count(*) FROM users"))).scalar_one()
    orgs_count = (await session.execute(text("SELECT count(*) FROM orgs"))).scalar_one()
    projects_count = (
        await session.execute(text("SELECT count(*) FROM projects"))
    ).scalar_one()
    issues_count = (
        await session.execute(text("SELECT count(*) FROM issues"))
    ).scalar_one()
    events_last_24h = (
        await session.execute(
            text(
                "SELECT count(*) FROM events "
                "WHERE received_at >= now() - make_interval(hours => :h)"
            ),
            {"h": int(_EVENTS_WINDOW.total_seconds() // 3600)},
        )
    ).scalar_one()

    partition_rows = (
        await session.execute(
            text(
                "SELECT c.relname AS name, "
                "greatest(c.reltuples, 0)::bigint AS row_estimate "
                "FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE p.relname = 'events' AND c.relname ~ :pat "
                "ORDER BY c.relname"
            ),
            {"pat": _PARTITION_NAME_PATTERN},
        )
    ).all()

    return {
        "users_count": int(users_count),
        "orgs_count": int(orgs_count),
        "projects_count": int(projects_count),
        "issues_count": int(issues_count),
        "events_last_24h": int(events_last_24h),
        "partitions": [
            {"name": row.name, "row_estimate": int(row.row_estimate)}
            for row in partition_rows
        ],
    }


async def get_overview(
    database_url: str,
    redis_url: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """Return the single operator-overview payload for the admin landing page.

    Resilient by design (this is the operator's "is the box healthy?" view): if
    the database is unreachable the counts fall back to zeros with
    ``db_ok=False`` rather than failing the whole request, and likewise the
    queue depth is None with ``redis_ok=False`` when Redis is down.
    """
    db_ok = await check_database(database_url)
    redis_ok = await check_redis(redis_url)

    stats: dict = {
        "users_count": 0,
        "orgs_count": 0,
        "projects_count": 0,
        "issues_count": 0,
        "events_last_24h": 0,
        "partitions": [],
    }
    if db_ok:
        try:
            async with admin_session(session_factory=session_factory) as session:
                stats = await _read_counts_and_partitions(session)
        except Exception:
            # A read failure after the probe passed: report the box as degraded
            # rather than 500 the operator's own status page. No detail logged
            # (it can carry connection strings).
            logger.warning("admin overview: stats read failed")
            db_ok = False

    queue_depth = await _queue_depth(redis_url) if redis_ok else None

    return {
        **stats,
        "queue_depth": queue_depth,
        "db_ok": db_ok,
        "redis_ok": redis_ok,
    }


async def list_orgs(
    page: int,
    per_page: int,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """Return a page of orgs with their member and project counts (cross-tenant).

    Ordered newest-first. Counts come from correlated subqueries so an org with
    zero members or projects still appears (an inner join would drop it).
    """
    offset = (page - 1) * per_page
    async with admin_session(session_factory=session_factory) as session:
        total = (await session.execute(text("SELECT count(*) FROM orgs"))).scalar_one()
        rows = (
            await session.execute(
                text(
                    "SELECT o.id AS id, o.name AS name, o.slug AS slug, "
                    "o.created_at AS created_at, "
                    "(SELECT count(*) FROM org_memberships m WHERE m.org_id = o.id) "
                    "AS member_count, "
                    "(SELECT count(*) FROM projects p WHERE p.org_id = o.id) "
                    "AS project_count "
                    "FROM orgs o "
                    "ORDER BY o.created_at DESC, o.id "
                    "LIMIT :limit OFFSET :offset"
                ),
                {"limit": per_page, "offset": offset},
            )
        ).all()
    return {
        "orgs": [
            {
                "id": str(row.id),
                "name": row.name,
                "slug": row.slug,
                "created_at": row.created_at,
                "member_count": int(row.member_count),
                "project_count": int(row.project_count),
            }
            for row in rows
        ],
        "total": int(total),
        "page": page,
        "per_page": per_page,
    }


async def list_users(
    page: int,
    per_page: int,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """Return a page of users (id, email, created_at, is_instance_admin, last_login_at).

    Ordered newest-first. ``users`` has no tenant scope, but reading it through
    ``admin_session`` keeps every panel read on one consistent role.
    """
    offset = (page - 1) * per_page
    async with admin_session(session_factory=session_factory) as session:
        total = (await session.execute(text("SELECT count(*) FROM users"))).scalar_one()
        rows = (
            await session.execute(
                text(
                    "SELECT id, email, created_at, is_instance_admin, last_login_at "
                    "FROM users ORDER BY created_at DESC, id "
                    "LIMIT :limit OFFSET :offset"
                ),
                {"limit": per_page, "offset": offset},
            )
        ).all()
    return {
        "users": [
            {
                "id": str(row.id),
                "email": row.email,
                "created_at": row.created_at,
                "is_instance_admin": bool(row.is_instance_admin),
                "last_login_at": row.last_login_at,
            }
            for row in rows
        ],
        "total": int(total),
        "page": page,
        "per_page": per_page,
    }


class LastAdminError(Exception):
    """Raised when a toggle would leave the instance with no instance admin."""


class NoSuchUserError(Exception):
    """Raised when the toggle targets a user id that does not exist."""


async def set_instance_admin(
    target_user_id: str,
    enabled: bool,
    acting_user_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """Toggle ``users.is_instance_admin`` for ``target_user_id``.

    The ONLY write in the panel. Runs on a plain ``crashlens_app`` session (the
    RLS-exempt ``users`` table), in one transaction with the last-admin guard so
    the count and the update cannot race a concurrent demotion:

    * ``LastAdminError`` if the caller tries to remove their OWN flag while they
      are the last instance admin (would orphan the instance).
    * ``NoSuchUserError`` if no such user exists.

    Returns the target user's ``{id, email, is_instance_admin}`` after the write.
    """
    factory = session_factory or get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            admin_count = (
                await session.execute(
                    text("SELECT count(*) FROM users WHERE is_instance_admin = true")
                )
            ).scalar_one()
            is_self = str(target_user_id) == str(acting_user_id)
            if would_orphan_instance(
                enabled=enabled, is_self=is_self, admin_count=int(admin_count)
            ):
                raise LastAdminError(
                    "You are the last instance administrator; you cannot remove "
                    "your own access. Grant it to someone else first."
                )
            row = (
                await session.execute(
                    text(
                        "UPDATE users SET is_instance_admin = :enabled "
                        "WHERE id = :id "
                        "RETURNING id, email, is_instance_admin"
                    ),
                    {"enabled": enabled, "id": str(target_user_id)},
                )
            ).one_or_none()
            if row is None:
                raise NoSuchUserError(str(target_user_id))
    return {
        "id": str(row.id),
        "email": row.email,
        "is_instance_admin": bool(row.is_instance_admin),
    }
