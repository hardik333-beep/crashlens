"""Reachability probes for the /health endpoint.

Each probe is defensive: any failure (unreachable service, bad credentials,
timeout) is reported as False rather than raising, so /health always returns a
document and never depends on a live datastore. This is what lets the smoke
test run without a database or Redis present.
"""

import asyncio
import logging

import redis.asyncio as redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 2.0


async def check_database(database_url: str) -> bool:
    """Return True if a trivial query against the database succeeds."""
    engine = create_async_engine(database_url, pool_pre_ping=False)
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        # Do not log the URL or exception detail: it can carry credentials.
        logger.warning("database health probe failed")
        return False
    finally:
        await engine.dispose()


async def check_redis(redis_url: str) -> bool:
    """Return True if Redis responds to PING."""
    client = redis.from_url(redis_url)
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            return bool(await client.ping())
    except Exception:
        logger.warning("redis health probe failed")
        return False
    finally:
        await client.aclose()
