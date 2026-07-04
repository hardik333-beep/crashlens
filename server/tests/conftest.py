"""Test configuration.

Provides throwaway values for the required settings so the application can be
constructed without a live environment. These are NOT real credentials; the
health probes will simply report the datastores as unreachable, which is the
behaviour the smoke test asserts is safe.

Also provides :func:`ensure_events_partitions`, the shared partition
provisioner for db-marked tests that insert ``events`` rows.
"""

import datetime
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://crashlens:crashlens@localhost:5432/crashlens",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-not-a-real-key")
os.environ.setdefault("ENVIRONMENT", "test")


async def ensure_events_partitions(engine_or_session, days: list[datetime.date]) -> None:
    """Ensure the daily ``events`` partitions for ``days`` exist (test provisioning).

    Migration 0001 pre-creates partitions only for CURRENT_DATE..+7 at deploy
    time, and other tests in the same CI session can legitimately drop or
    predate that window (the partition-function and retention tests exercise
    partition drops), so any db-marked test that inserts ``events`` rows must
    provision the exact partitions it needs instead of assuming the
    deploy-time window survived.

    Calls the idempotent SQL function ``create_events_partition`` from
    migration 0001. Execution role: since revision 0003 the function is
    SECURITY DEFINER with EXECUTE granted to ``crashlens_app``, and the test
    fixtures' ``crashlens_test`` login role is a member of ``crashlens_app``,
    so this works from either the superuser engine or the non-superuser test
    role. Accepts an ``AsyncEngine`` (opens its own transaction) or an
    ``AsyncSession`` already inside one.
    """
    statement = text("SELECT create_events_partition(:day)")
    if isinstance(engine_or_session, AsyncEngine):
        async with engine_or_session.begin() as conn:
            for day in days:
                await conn.execute(statement, {"day": day})
    else:
        for day in days:
            await engine_or_session.execute(statement, {"day": day})
