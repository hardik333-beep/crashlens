"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings, get_settings
from app.routes.admin import router as admin_router
from app.routes.alerts import router as alerts_router
from app.routes.audit import router as audit_router
from app.routes.auth import router as auth_router
from app.routes.health import router as health_router
from app.routes.ingest import router as ingest_router
from app.routes.issues import router as issues_router
from app.routes.orgs import router as orgs_router
from app.routes.projects import router as projects_router
from app.routes.sourcemaps import router as sourcemaps_router


class _ApiPrefixStripMiddleware:
    """Strip a leading ``/api`` from the request path, in-process.

    In the compose stack the Caddy reverse proxy strips ``/api`` before it
    forwards to the API (``handle_path /api/*`` in ``deploy/Caddyfile``), which is
    why every API router is mounted WITHOUT an ``/api`` prefix. Single-container
    mode has no Caddy, so this middleware reproduces exactly that one behaviour:
    the dashboard's ``GET /api/health`` reaches the ``/health`` route, and
    ``POST /api/ingest/{id}/`` reaches ``/ingest/{id}/``. It only rewrites the
    path in the ASGI scope (no body buffering), so the ingest hot path is
    unaffected. It is added only when the dashboard is served from this process.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/api" or path.startswith("/api/"):
                scope = dict(scope)
                scope["path"] = path[len("/api") :] or "/"
                raw_path = scope.get("raw_path")
                if isinstance(raw_path, bytes) and raw_path.startswith(b"/api"):
                    scope["raw_path"] = raw_path[len(b"/api") :] or b"/"
        await self.app(scope, receive, send)


def _mount_dashboard(app: FastAPI, directory: str) -> None:
    """Serve the compiled dashboard SPA from the API process (single-container mode).

    Mounts the Vite build's hashed assets under ``/assets`` and adds a catch-all
    GET that returns ``index.html`` for every other path so client-side routing
    works. The API routers were already included on ``app`` before this call, so
    ``/health`` and the other API routes (reachable at ``/api/...`` via
    :class:`_ApiPrefixStripMiddleware`) take precedence over the SPA catch-all;
    the catch-all only answers paths no API route claimed.
    """
    dist = Path(directory)
    index_file = dist / "index.html"
    assets_dir = dist / "assets"

    app.add_middleware(_ApiPrefixStripMiddleware)
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _serve_spa(full_path: str) -> FileResponse:  # noqa: ARG001 - path captured for routing only
        return FileResponse(index_file)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the shutdown of the shared arq Redis pool.

    The ingest hot path creates one arq pool lazily on first use and stores it on
    ``app.state.arq_pool`` (see ``app.routes.ingest.get_ingest_pool``). Creation
    stays lazy so a cold Redis never blocks startup; this lifespan just closes the
    pool cleanly on shutdown if one was ever created.
    """
    try:
        yield
    finally:
        pool = getattr(app.state, "arq_pool", None)
        if pool is not None:
            await pool.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure a Crashlens API application.

    Passing ``settings`` explicitly is used by tests; in production the app
    reads configuration from the environment via ``get_settings``.
    """
    app = FastAPI(title="Crashlens", version="0.0.1", lifespan=_lifespan)
    app.state.settings = settings or get_settings()

    # Routes are mounted without an /api prefix: the reverse proxy strips /api
    # before forwarding, so GET /api/health from the browser reaches /health here.
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(orgs_router)
    app.include_router(projects_router)
    app.include_router(sourcemaps_router)
    app.include_router(issues_router)
    app.include_router(ingest_router)
    app.include_router(alerts_router)
    app.include_router(audit_router)
    app.include_router(admin_router)

    # Single-container mode (opt-in): if SERVE_DASHBOARD_DIR points at a compiled
    # dashboard build, this process also serves the SPA and answers /api/* itself.
    # Unset (the default and the compose stack's behaviour) leaves the app
    # byte-identical: no static mount, no /api strip, and unknown paths still 404.
    dashboard_dir = app.state.settings.serve_dashboard_dir
    if dashboard_dir and Path(dashboard_dir).is_dir():
        _mount_dashboard(app, dashboard_dir)

    return app


app = create_app()
