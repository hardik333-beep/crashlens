"""arq worker entrypoint.

Connects to Redis, runs the ``process_event`` consumer that fingerprints
ingested events into Issues, and the partition/retention cron jobs. Run it with:

    arq app.worker.WorkerSettings
"""

import datetime
import logging

from arq import func
from arq.connections import RedisSettings
from arq.cron import cron

from app.config import get_settings
from app.jobs.alerts import dispatch_alerts
from app.jobs.process_event import process_event
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

    # dispatch_alerts is wrapped with a lower max_tries: an alert fan-out that
    # partially failed should retry a bounded number of times (per-channel
    # isolation already prevents one bad channel from failing the whole job), not
    # re-notify every channel repeatedly on the default 3-try budget. FLAGGED
    # DEFAULT (governor review): max_tries=2 for dispatch_alerts.
    functions: list = [process_event, func(dispatch_alerts, max_tries=2)]
    cron_jobs = [
        cron(maintain_event_partitions, hour=0, minute=10),
        cron(enforce_retention, hour=0, minute=30),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    timezone = datetime.UTC
    # FLAGGED DEFAULTS (governor review): retry a failed job up to 3 times (a
    # transient DB/Redis blip should recover; a poison event is already caught
    # inside process_event so it never reaches these retries), and cap any single
    # job at 60s so a pathological event cannot pin a worker slot indefinitely.
    max_tries = 3
    job_timeout = 60
