"""Unit tests for the pure logic in the projects slice (no database).

Covers project slug generation and DSN public-key generation, plus the
unauthenticated / non-member short circuits on the project endpoints that reject
before any database access is required.
"""

import re
import uuid

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app import projects, security
from app.main import create_app
from app.routes.projects import _validate_sampling_rate


# --- Project slug -------------------------------------------------------------
def test_project_slug_is_urlsafe_and_prefixed_from_name() -> None:
    slug = projects._make_project_slug("My Web App!")
    # Structural, deterministic assertions. The random 8-char suffix comes from
    # secrets.token_urlsafe (base64.urlsafe_b64encode), whose alphabet is
    # A-Za-z0-9 plus "-" and "_", so the suffix itself may contain hyphens:
    # never split the slug on "-" to recover the base.
    # Shape: normalized lowercase base + "-" + exactly 8 suffix characters.
    base = "my-web-app"
    assert slug.startswith(base + "-")
    assert len(slug) == len(base) + 1 + 8
    assert re.fullmatch(r"[A-Za-z0-9_-]+", slug) is not None


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


# --- Sampling rate PATCH bounds validation (W6-04) -----------------------------
@pytest.mark.parametrize("rate", [0.0, 0.25, 0.5, 1.0])
def test_valid_sampling_rate_passes(rate: float) -> None:
    _validate_sampling_rate(rate)  # must not raise


@pytest.mark.parametrize("rate", [-0.01, 1.01, -1.0, 2.0])
def test_out_of_bounds_sampling_rate_raises_400(rate: float) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _validate_sampling_rate(rate)
    assert exc_info.value.status_code == 400
