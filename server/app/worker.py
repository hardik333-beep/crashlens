"""arq worker entrypoint.

Connects to Redis and runs the registered cron jobs (no on-demand job
functions yet -- those arrive in a later slice). Run it with:

    arq app.worker.WorkerSettings
"""

import datetime
import logging

from arq.connections import RedisSettings
from arq.cron import cron

from app.config import get_settings
from app.jobs.retention import enforce_retention, maintain_event_partitions

logger = logging.getLogger(__name__)


async def startup(ctx: dict) -> None:
    logger.info("crashlens worker starting")


async def shutdown(ctx: dict) -> None:
    logger.info("crashlens worker shutting down")


class WorkerSettings:
    """arq configuration: partition maintenance and retention cron jobs.

    Both times are FLAGGED DEFAULTS chosen by this slice (governor review
    requested): 00:10 UTC for partition maintenance and 00:30 UTC for
    retention, staggered so retention's partition drop never races
    maintenance's partition create in the same minute. ``timezone`` is set
    explicitly to UTC so these hours are not reinterpreted against whatever
    local timezone the host/container happens to be configured with (arq
    otherwise defaults ``cron`` evaluation to system time).
    """

    functions: list = []
    cron_jobs = [
        cron(maintain_event_partitions, hour=0, minute=10),
        cron(enforce_retention, hour=0, minute=30),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    timezone = datetime.UTC
