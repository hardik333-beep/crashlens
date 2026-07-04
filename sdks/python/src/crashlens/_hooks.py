"""Global excepthook installation for unhandled exceptions.

Installs both ``sys.excepthook`` (main thread) and ``threading.excepthook``
(worker threads), chaining whatever hook was previously installed so we never
swallow another integration's handler or the default traceback print.
"""

from __future__ import annotations

import logging
import sys
import threading

logger = logging.getLogger("crashlens")

_installed = False
_prev_sys_excepthook = None
_prev_threading_excepthook = None


def install(client) -> None:  # noqa: ANN001 - avoids an import cycle with _client
    """Install the excepthooks once. Safe to call more than once."""
    global _installed, _prev_sys_excepthook, _prev_threading_excepthook
    if _installed:
        return
    _installed = True

    _prev_sys_excepthook = sys.excepthook

    def sys_hook(exc_type, exc_value, exc_tb):
        try:
            client.capture_exception(
                (exc_type, exc_value, exc_tb), level="fatal"
            )
            client.flush(2.0)
        except Exception:  # noqa: BLE001 - never interfere with shutdown
            logger.debug("crashlens: sys excepthook capture failed", exc_info=True)
        if _prev_sys_excepthook is not None:
            _prev_sys_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = sys_hook

    _prev_threading_excepthook = getattr(threading, "excepthook", None)

    def thread_hook(args):
        try:
            if args.exc_value is not None:
                client.capture_exception(
                    (args.exc_type, args.exc_value, args.exc_traceback),
                    level="fatal",
                )
        except Exception:  # noqa: BLE001
            logger.debug(
                "crashlens: threading excepthook capture failed", exc_info=True
            )
        if _prev_threading_excepthook is not None:
            _prev_threading_excepthook(args)

    if hasattr(threading, "excepthook"):
        threading.excepthook = thread_hook


def _reset_for_tests() -> None:
    """Restore the pre-install hooks. Test-only helper."""
    global _installed, _prev_sys_excepthook, _prev_threading_excepthook
    if _prev_sys_excepthook is not None:
        sys.excepthook = _prev_sys_excepthook
    if _prev_threading_excepthook is not None and hasattr(threading, "excepthook"):
        threading.excepthook = _prev_threading_excepthook
    _installed = False
    _prev_sys_excepthook = None
    _prev_threading_excepthook = None
