"""arq worker entrypoint.

Skeleton only: it connects to Redis and starts a consumer with no jobs
registered yet. Job functions are added in a later slice. Run it with:

    arq app.worker.WorkerSettings
"""

import logging

from arq.connections import RedisSettings

from app.config import get_settings

logger = logging.getLogger(__name__)


async def startup(ctx: dict) -> None:
    logger.info("crashlens worker starting")


async def shutdown(ctx: dict) -> None:
    logger.info("crashlens worker shutting down")


class WorkerSettings:
    """arq configuration. No job functions are registered yet."""

    functions: list = []
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
