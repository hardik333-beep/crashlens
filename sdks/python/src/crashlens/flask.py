"""Flask integration.

Connect Crashlens to a Flask app so unhandled request exceptions are reported
with the request URL and method::

    from crashlens.flask import CrashlensFlask
    CrashlensFlask(app)

    # or, functional style:
    import crashlens.flask
    crashlens.flask.init_app(app)

Flask is imported lazily inside the functions, so importing this module without
Flask installed does not fail; calling the integration without Flask raises a
clear error.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import get_client
from ._client import Client

logger = logging.getLogger("crashlens")


def _resolve_client(client: Optional[Client]) -> Optional[Client]:
    return client if client is not None else get_client()


def init_app(app: Any, client: Optional[Client] = None) -> None:
    """Wire Crashlens into a Flask ``app`` via the request-exception signal."""
    try:
        from flask import got_request_exception, request as flask_request
    except ImportError as exc:  # pragma: no cover - exercised only without flask
        raise RuntimeError(
            "crashlens.flask requires Flask to be installed"
        ) from exc

    def _handler(sender: Any, exception: BaseException, **_extra: Any) -> None:
        try:
            resolved = _resolve_client(client)
            if resolved is None:
                return
            request_data = None
            try:
                request_data = {
                    "url": flask_request.url,
                    "method": flask_request.method,
                }
            except Exception:  # noqa: BLE001 - outside a request context
                request_data = None
            resolved.capture_exception(
                exception, level="error", request=request_data
            )
        except Exception:  # noqa: BLE001 - a signal handler must not raise
            logger.debug("crashlens: flask handler failed", exc_info=True)

    # weak=False keeps the closure alive for the life of the app.
    got_request_exception.connect(_handler, app, weak=False)


class CrashlensFlask:
    """Flask extension object. Pass ``app`` to wire immediately, or call
    :meth:`init_app` later (application-factory pattern)."""

    def __init__(
        self, app: Optional[Any] = None, client: Optional[Client] = None
    ) -> None:
        self._client = client
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Any) -> None:
        init_app(app, client=self._client)
