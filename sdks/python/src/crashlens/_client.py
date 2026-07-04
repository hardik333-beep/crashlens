"""The Crashlens client: config plus capture entry points.

Every public method is exception-safe: a failure while building or sending an
event is logged at debug level to the ``crashlens`` logger and swallowed, so the
SDK can never take down the host application.
"""

from __future__ import annotations

import atexit
import logging
import sys
from typing import Any, Dict, Optional, Sequence

from . import _envelope
from ._transport import Transport
from ._version import __version__

logger = logging.getLogger("crashlens")

ExcInput = Any  # BaseException | (type, value, tb) | None


def level_from_logging(levelno: int) -> str:
    """Map a stdlib logging level number to a protocol level string."""
    if levelno >= logging.CRITICAL:
        return "fatal"
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warning"
    if levelno >= logging.INFO:
        return "info"
    return "debug"


def _normalize_exc_info(exc: ExcInput) -> Optional[_envelope.ExcInfo]:
    """Coerce an exception, an exc_info tuple, or None into an exc_info tuple."""
    if exc is None:
        info = sys.exc_info()
        return info if info[1] is not None else None
    if isinstance(exc, BaseException):
        return (type(exc), exc, exc.__traceback__)
    if isinstance(exc, tuple) and len(exc) == 3:
        if exc[1] is None:
            return None
        return exc  # type: ignore[return-value]
    return None


class Client:
    def __init__(
        self,
        url: str,
        key: str,
        *,
        environment: str = "production",
        release: Optional[str] = None,
        in_app_module_prefixes: Optional[Sequence[str]] = None,
        max_queue: int = 100,
        timeout: float = 2.0,
    ) -> None:
        self.environment = environment
        self.release = release
        self.in_app_module_prefixes = (
            tuple(in_app_module_prefixes) if in_app_module_prefixes else None
        )
        self._transport = Transport(
            url, key, timeout=timeout, max_queue=max_queue
        )
        atexit.register(self._atexit_flush)

    # -- capture -----------------------------------------------------------

    def capture_exception(
        self,
        exc: ExcInput = None,
        *,
        level: str = "error",
        message: Optional[str] = None,
        request: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Capture an exception. Returns the event id, or None if nothing sent."""
        try:
            exc_info = _normalize_exc_info(exc)
            if exc_info is None:
                return None
            event = _envelope.build_event(
                sdk_version=__version__,
                environment=self.environment,
                release=self.release,
                prefixes=self.in_app_module_prefixes,
                level=level,
                message=message,
                exc_info=exc_info,
                request=request,
            )
            self._transport.submit(event)
            return event["event_id"]
        except Exception:  # noqa: BLE001 - capture must never raise
            logger.debug("crashlens: capture_exception failed", exc_info=True)
            return None

    def capture_message(
        self,
        message: str,
        *,
        level: str = "info",
        request: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Capture a log-style message with no exception."""
        try:
            event = _envelope.build_event(
                sdk_version=__version__,
                environment=self.environment,
                release=self.release,
                prefixes=self.in_app_module_prefixes,
                level=level,
                message=message,
                exc_info=None,
                request=request,
            )
            self._transport.submit(event)
            return event["event_id"]
        except Exception:  # noqa: BLE001 - capture must never raise
            logger.debug("crashlens: capture_message failed", exc_info=True)
            return None

    # -- lifecycle ---------------------------------------------------------

    def flush(self, timeout: float = 5.0) -> bool:
        try:
            return self._transport.flush(timeout)
        except Exception:  # noqa: BLE001
            logger.debug("crashlens: flush failed", exc_info=True)
            return False

    def close(self, timeout: float = 5.0) -> None:
        try:
            self._transport.close(timeout)
        except Exception:  # noqa: BLE001
            logger.debug("crashlens: close failed", exc_info=True)

    def _atexit_flush(self) -> None:
        try:
            self._transport.flush(5.0)
        except Exception:  # noqa: BLE001
            pass
