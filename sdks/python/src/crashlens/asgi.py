"""ASGI middleware for FastAPI, Starlette, and any ASGI app.

Wrap your application to report unhandled exceptions with request context::

    from crashlens.asgi import CrashlensMiddleware
    app.add_middleware(CrashlensMiddleware)  # FastAPI / Starlette

    # or wrap directly:
    app = CrashlensMiddleware(app)

The exception is captured and then RE-RAISED so the framework's own error
handling still runs. No framework import is required.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote

from . import get_client
from ._client import Client

logger = logging.getLogger("crashlens")

Scope = dict
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]


def _url_from_scope(scope: Scope) -> Optional[str]:
    try:
        scheme = scope.get("scheme", "http")
        path = scope.get("path", "")
        raw_query = scope.get("query_string", b"")
        query = raw_query.decode("latin-1") if raw_query else ""

        host = None
        for name, value in scope.get("headers", []) or []:
            if name == b"host":
                host = value.decode("latin-1")
                break
        if host is None:
            server = scope.get("server")
            if server:
                host = server[0]
                if server[1] and server[1] not in (80, 443):
                    host = f"{host}:{server[1]}"
        if host is None:
            return path or None

        url = f"{scheme}://{host}{quote(path, safe='/%:@')}"
        if query:
            url = f"{url}?{query}"
        return url
    except Exception:  # noqa: BLE001 - URL building is best-effort
        return None


class CrashlensMiddleware:
    def __init__(self, app: Any, client: Optional[Client] = None) -> None:
        self.app = app
        self._client = client

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            try:
                resolved = self._client if self._client is not None else get_client()
                if resolved is not None:
                    request_data = {
                        "url": _url_from_scope(scope),
                        "method": scope.get("method"),
                    }
                    resolved.capture_exception(
                        exc, level="error", request=request_data
                    )
            except Exception:  # noqa: BLE001 - capture must not mask the error
                logger.debug("crashlens: asgi capture failed", exc_info=True)
            raise
