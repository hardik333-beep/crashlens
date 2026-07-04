"""Unit tests for the pure logic in the projects slice (no database).

Covers project slug generation and DSN public-key generation, plus the
unauthenticated / non-member short circuits on the project endpoints that reject
before any database access is required.
"""

import uuid

from httpx import ASGITransport, AsyncClient

from app import projects, security
from app.main import create_app


# --- Project slug -------------------------------------------------------------
def test_project_slug_is_urlsafe_and_prefixed_from_name() -> None:
    slug = projects._make_project_slug("My Web App!")
    # Normalized, lowercased prefix with non-alphanumerics collapsed to hyphens,
    # then a short random url-safe suffix (same shape as accounts._make_org_slug).
    prefix, _, suffix = slug.rpartition("-")
    assert prefix == "my-web-app"
    assert all(c.islower() or c.isdigit() or c == "-" for c in prefix)
    assert suffix and all(c.isalnum() or c in "-_" for c in suffix)


def test_project_slug_is_unique_per_call() -> None:
    first = projects._make_project_slug("Same Name")
    second = projects._make_project_slug("Same Name")
    assert first != second
    assert first.startswith("same-name-")
    assert second.startswith("same-name-")


def test_project_slug_falls_back_when_name_has_no_alphanumerics() -> None:
    slug = projects._make_project_slug("!!!")
    assert slug.startswith("project-")


# --- DSN public key -----------------------------------------------------------
def test_public_keys_are_unique_and_urlsafe() -> None:
    key_a = security.generate_public_key()
    key_b = security.generate_public_key()
    assert key_a != key_b
    # token_urlsafe alphabet: letters, digits, '-' and '_'.
    assert all(c.isalnum() or c in "-_" for c in key_a)
    assert len(key_a) > 20


# --- Unauthenticated access short circuits before the database ----------------
async def test_list_projects_without_token_is_401() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/orgs/{uuid.uuid4()}/projects")
    assert response.status_code == 401


async def test_create_project_without_token_is_401() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/orgs/{uuid.uuid4()}/projects", json={"name": "Anything"}
        )
    assert response.status_code == 401


async def test_members_without_token_is_401() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/orgs/{uuid.uuid4()}/members")
    assert response.status_code == 401
