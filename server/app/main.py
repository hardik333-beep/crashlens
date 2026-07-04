"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.routes.alerts import router as alerts_router
from app.routes.auth import router as auth_router
from app.routes.health import router as health_router
from app.routes.ingest import router as ingest_router
from app.routes.issues import router as issues_router
from app.routes.orgs import router as orgs_router
from app.routes.projects import router as projects_router
from app.routes.sourcemaps import router as sourcemaps_router


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

    return app


app = create_app()
