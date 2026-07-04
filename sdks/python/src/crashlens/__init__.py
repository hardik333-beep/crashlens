"""Crashlens Python SDK.

One-line setup::

    import crashlens
    crashlens.init("https://<public_key>@your-host/api/ingest/<project_id>/")

After :func:`init`, unhandled exceptions are reported automatically and you can
capture handled errors, messages, breadcrumbs, tags, and user context through
the module-level API. Everything is non-blocking and never raises into your app.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence

from . import _dsn, _hooks, _scope
from ._client import Client
from ._version import __version__

logger = logging.getLogger("crashlens")

__all__ = [
    "init",
    "Client",
    "capture_exception",
    "capture_message",
    "add_breadcrumb",
    "set_tag",
    "set_user",
    "flush",
    "get_client",
    "__version__",
]

_default_client: Optional[Client] = None


def init(
    dsn: Optional[str] = None,
    *,
    url: Optional[str] = None,
    key: Optional[str] = None,
    environment: str = "production",
    release: Optional[str] = None,
    in_app_module_prefixes: Optional[Sequence[str]] = None,
    max_queue: int = 100,
    timeout: float = 2.0,
    install_excepthooks: bool = True,
) -> Client:
    """Initialise the SDK and return the default :class:`Client`.

    Pass either a ``dsn`` string or an explicit ``url`` + ``key`` pair. Idempotent
    in the sense that calling it again replaces the module-level default client.
    """
    global _default_client
    dsn_parts = _dsn.resolve(dsn, url=url, key=key)
    client = Client(
        dsn_parts.url,
        dsn_parts.key,
        environment=environment,
        release=release,
        in_app_module_prefixes=in_app_module_prefixes,
        max_queue=max_queue,
        timeout=timeout,
    )
    _default_client = client
    if install_excepthooks:
        _hooks.install(client)
    return client


def get_client() -> Optional[Client]:
    """Return the module-level default client, or None if :func:`init` was not run."""
    return _default_client


# -- module-level capture API (all safe no-ops before init) ----------------


def capture_exception(exc: Any = None) -> Optional[str]:
    """Capture an exception (defaults to the one currently being handled)."""
    client = _default_client
    if client is None:
        return None
    return client.capture_exception(exc)


def capture_message(message: str, level: str = "info") -> Optional[str]:
    """Capture a log-style message with no exception."""
    client = _default_client
    if client is None:
        return None
    return client.capture_message(message, level=level)


def add_breadcrumb(
    message: Optional[str] = None,
    *,
    type: Optional[str] = None,  # noqa: A002 - matches protocol field name
    category: Optional[str] = None,
    level: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a breadcrumb on the current context (async/thread safe)."""
    try:
        _scope.add_breadcrumb(
            message, type=type, category=category, level=level, data=data
        )
    except Exception:  # noqa: BLE001 - never raise into the host app
        logger.debug("crashlens: add_breadcrumb failed", exc_info=True)


def set_tag(key: str, value: Any) -> None:
    """Set a string tag on the current context."""
    try:
        _scope.set_tag(key, value)
    except Exception:  # noqa: BLE001
        logger.debug("crashlens: set_tag failed", exc_info=True)


def set_user(id: Optional[str]) -> None:  # noqa: A002 - matches protocol field name
    """Set (or clear) the current user's id."""
    try:
        _scope.set_user(id)
    except Exception:  # noqa: BLE001
        logger.debug("crashlens: set_user failed", exc_info=True)


def flush(timeout: float = 5.0) -> bool:
    """Block until queued events are sent or ``timeout`` elapses."""
    client = _default_client
    if client is None:
        return True
    return client.flush(timeout)
