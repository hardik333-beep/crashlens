"""Logging integration.

Two handlers:

* :class:`CrashlensHandler` forwards log records at or above its level to
  Crashlens as events (an exception event when the record carries ``exc_info``,
  otherwise a message event)::

      import logging
      from crashlens.logging import CrashlensHandler
      logging.getLogger().addHandler(CrashlensHandler(level=logging.ERROR))

* :class:`CrashlensBreadcrumbHandler` (opt-in) turns lower-level log records
  into breadcrumbs, so the trail leading up to an error travels with it::

      logging.getLogger().addHandler(CrashlensBreadcrumbHandler(level=logging.INFO))

Both ignore records from the ``crashlens`` logger itself to avoid recursion.
"""

from __future__ import annotations

import logging
from typing import Optional

from . import add_breadcrumb, get_client
from ._client import Client, level_from_logging


def _is_own_record(record: logging.LogRecord) -> bool:
    return record.name == "crashlens" or record.name.startswith("crashlens.")


class CrashlensHandler(logging.Handler):
    """A logging handler that sends records to Crashlens as events."""

    def __init__(
        self, level: int = logging.ERROR, client: Optional[Client] = None
    ) -> None:
        super().__init__(level=level)
        self._client = client

    def emit(self, record: logging.LogRecord) -> None:
        if _is_own_record(record):
            return
        try:
            client = self._client if self._client is not None else get_client()
            if client is None:
                return
            level = level_from_logging(record.levelno)
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                client.capture_exception(
                    record.exc_info, level=level, message=message
                )
            else:
                client.capture_message(message, level=level)
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


class CrashlensBreadcrumbHandler(logging.Handler):
    """A logging handler that records log lines as breadcrumbs (opt-in)."""

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord) -> None:
        if _is_own_record(record):
            return
        try:
            add_breadcrumb(
                record.getMessage(),
                type="log",
                category=record.name,
                level=level_from_logging(record.levelno),
            )
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)
