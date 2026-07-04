"""FastAPI application factory."""

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.routes.auth import router as auth_router
from app.routes.health import router as health_router
from app.routes.orgs import router as orgs_router
from app.routes.projects import router as projects_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure a Crashlens API application.

    Passing ``settings`` explicitly is used by tests; in production the app
    reads configuration from the environment via ``get_settings``.
    """
    app = FastAPI(title="Crashlens", version="0.0.1")
    app.state.settings = settings or get_settings()

    # Routes are mounted without an /api prefix: the reverse proxy strips /api
    # before forwarding, so GET /api/health from the browser reaches /health here.
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(orgs_router)
    app.include_router(projects_router)

    return app


app = create_app()
