"""Tests for single-container mode (SERVE_DASHBOARD_DIR).

These exercise the app through an in-process ASGI transport, so they need no
running server, database, or Redis. Two things are proven:

* The default (SERVE_DASHBOARD_DIR unset) leaves the app byte-identical: no SPA
  is mounted and an unknown path still 404s.
* When SERVE_DASHBOARD_DIR points at a build, the SPA index.html is served for
  unknown paths, real assets are served, and the /api prefix is stripped so the
  dashboard's /api/* calls reach the API routes WITHOUT the SPA shadowing them.
"""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app

_BASE_SETTINGS = {
    "database_url": "postgresql+asyncpg://crashlens:crashlens@localhost:5432/crashlens",
    "redis_url": "redis://localhost:6379/0",
    "secret_key": "test-secret-not-a-real-key",
}


@pytest.fixture
def dashboard_dir(tmp_path: Path) -> Path:
    """Write a minimal Vite-style build (index.html + assets/) into a temp dir."""
    (tmp_path / "index.html").write_text("<!doctype html><title>Crashlens</title>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index-abc123.js").write_text("console.log('crashlens');")
    return tmp_path


async def test_default_has_no_spa_and_still_404s() -> None:
    # No serve_dashboard_dir: behaviour is unchanged from the base app.
    settings = Settings(**_BASE_SETTINGS)
    assert settings.serve_dashboard_dir is None
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # The health route still works at its bare path (no /api strip installed).
        health = await client.get("/health")
        assert health.status_code == 200
        # An unknown path is a real 404, not an index.html catch-all.
        missing = await client.get("/some/spa/route")
        assert missing.status_code == 404


async def test_serves_spa_and_does_not_shadow_api(dashboard_dir: Path) -> None:
    settings = Settings(**_BASE_SETTINGS, serve_dashboard_dir=str(dashboard_dir))
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # The SPA index.html is returned for the root and for client-side routes.
        root = await client.get("/")
        assert root.status_code == 200
        assert "<title>Crashlens</title>" in root.text
        spa_route = await client.get("/orgs/anything/issues")
        assert spa_route.status_code == 200
        assert "<title>Crashlens</title>" in spa_route.text

        # A hashed asset is served as a real file, not the index.html fallback.
        asset = await client.get("/assets/index-abc123.js")
        assert asset.status_code == 200
        assert "crashlens" in asset.text

        # The dashboard calls the API under /api; the prefix is stripped so the
        # request reaches the /health route instead of being shadowed by the SPA.
        api_health = await client.get("/api/health")
        assert api_health.status_code == 200
        assert set(api_health.json().keys()) == {"status", "database", "redis"}
