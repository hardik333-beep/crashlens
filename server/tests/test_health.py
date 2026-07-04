"""Smoke test for the /health endpoint.

Exercises the app through an in-process ASGI transport, so it does not require a
running server, database, or Redis. When the datastores are absent the
reachability booleans are simply False; the endpoint must still return 200.
"""

from httpx import ASGITransport, AsyncClient

from app.main import create_app


async def test_health_returns_ok_and_reachability_booleans() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body.keys()) == {"status", "database", "redis"}
    assert isinstance(body["database"], bool)
    assert isinstance(body["redis"], bool)
