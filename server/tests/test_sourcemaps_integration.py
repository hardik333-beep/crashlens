"""DB-marked integration tests for source map upload/list/delete + symbolication.

Two kinds, mirroring ``test_projects_integration.py``:

* HTTP-level through the ASGI app, proving the admin-only authZ matrix (member
  and outsider are refused), the upload -> list -> delete lifecycle, .map-only
  rejection, path-safety of the stored basename, and cross-org isolation (org B's
  admin cannot touch org A's project). The ``SOURCEMAPS_DIR`` setting is pointed
  at a temp directory for the duration of the test.

* Worker-level: store a real esbuild fixture map on disk, then run
  ``process_event`` on a synthetic ``javascript`` event and assert the stored
  Issue was fingerprinted on the ORIGINAL (symbolicated) frames.

These require a live PostgreSQL with the migrations applied and SKIP cleanly when
none is reachable, so ``pytest -q`` still passes locally without Postgres.
"""

import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import security, sourcemaps
from app.config import get_settings
from app.jobs.process_event import compute_fingerprint, normalize_envelope, process_event
from app.main import create_app
from tests.conftest import superuser_database_url

pytestmark = pytest.mark.db

_TEST_ROLE = "crashlens_test"
_TEST_PASSWORD = "crashlens_test"
_KNOWN_PASSWORD = "a-strong-test-passphrase"

_FIXTURE_MAP = os.path.join(
    os.path.dirname(__file__), "fixtures", "sourcemaps", "app.min.js.map"
)


@pytest_asyncio.fixture(scope="module")
async def superuser_engine():
    engine = create_async_engine(superuser_database_url())
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("PostgreSQL not reachable; skipping sourcemaps integration tests")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="module")
async def app_sessionmaker(superuser_engine):
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crashlens_test') "
                "THEN CREATE ROLE crashlens_test LOGIN PASSWORD 'crashlens_test' "
                "NOSUPERUSER; END IF; "
                "END $$;"
            )
        )
        await conn.execute(text("GRANT crashlens_app TO crashlens_test"))
        await conn.execute(text("GRANT crashlens_system TO crashlens_test"))
    url = make_url(superuser_database_url()).set(
        username=_TEST_ROLE, password=_TEST_PASSWORD
    )
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def maps_dir(tmp_path, monkeypatch):
    """Point the SOURCEMAPS_DIR setting at a temp dir for the test.

    ``get_settings`` is lru-cached, so we patch the attribute on the cached
    instance and restore is automatic (monkeypatch) at teardown.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "sourcemaps_dir", str(tmp_path))
    return str(tmp_path)


async def _seed_user(conn, email: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO users (id, email, password_hash) VALUES (:id, :email, :ph)"),
        {"id": user_id, "email": email, "ph": security.hash_password(_KNOWN_PASSWORD)},
    )
    return user_id


async def _seed_org(conn, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO orgs (id, name, slug) VALUES (:id, :name, :slug)"),
        {"id": org_id, "name": name, "slug": f"{name}-{org_id}"},
    )
    return org_id


async def _seed_project(conn, org_id: uuid.UUID, name: str) -> uuid.UUID:
    project_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO projects (id, org_id, name, slug, platform) "
            "VALUES (:id, :oid, :name, :slug, 'javascript')"
        ),
        {"id": project_id, "oid": org_id, "name": name, "slug": f"{name}-{project_id}"},
    )
    return project_id


async def _add_membership(conn, org_id, user_id, role) -> None:
    await conn.execute(
        text(
            "INSERT INTO org_memberships (org_id, user_id, role) "
            "VALUES (:oid, :uid, :role)"
        ),
        {"oid": org_id, "uid": user_id, "role": role},
    )


@pytest_asyncio.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _map_bytes() -> bytes:
    with open(_FIXTURE_MAP, "rb") as handle:
        return handle.read()


@pytest.mark.isolation
async def test_upload_list_delete_lifecycle_and_authz(
    superuser_engine, client, maps_dir
) -> None:
    admin_email = f"smadmin-{uuid.uuid4()}@example.test"
    member_email = f"smmember-{uuid.uuid4()}@example.test"
    outsider_email = f"smout-{uuid.uuid4()}@example.test"
    async with superuser_engine.begin() as conn:
        org_id = await _seed_org(conn, "SmCo")
        project_id = await _seed_project(conn, org_id, "web")
        admin_id = await _seed_user(conn, admin_email)
        member_id = await _seed_user(conn, member_email)
        outsider_id = await _seed_user(conn, outsider_email)
        await _add_membership(conn, org_id, admin_id, "admin")
        await _add_membership(conn, org_id, member_id, "member")
    admin_h = {"Authorization": f"Bearer {security.create_access_token(admin_id)}"}
    member_h = {"Authorization": f"Bearer {security.create_access_token(member_id)}"}
    outsider_h = {"Authorization": f"Bearer {security.create_access_token(outsider_id)}"}
    base = f"/orgs/{org_id}/projects/{project_id}/sourcemaps"
    try:
        # Member and outsider cannot list (admin-only).
        assert (await client.get(base, headers=member_h)).status_code == 403
        assert (await client.get(base, headers=outsider_h)).status_code == 403
        # Admin: empty list to start.
        r0 = await client.get(base, headers=admin_h)
        assert r0.status_code == 200
        assert r0.json() == []

        # Member cannot upload.
        deny = await client.post(
            base,
            data={"release": "web@1.4.2"},
            files={"files": ("app.min.js.map", _map_bytes(), "application/json")},
            headers=member_h,
        )
        assert deny.status_code == 403

        # A non-.map file is rejected with 400.
        bad = await client.post(
            base,
            data={"release": "web@1.4.2"},
            files={"files": ("app.min.js", _map_bytes(), "application/javascript")},
            headers=admin_h,
        )
        assert bad.status_code == 400

        # Admin uploads a real map. The client sends a traversal-y filename; the
        # server must store it under the safe basename only.
        ok = await client.post(
            base,
            data={"release": "web@1.4.2"},
            files={
                "files": ("../../evil/app.min.js.map", _map_bytes(), "application/json")
            },
            headers=admin_h,
        )
        assert ok.status_code == 201
        body = ok.json()
        assert body["release"] == "web@1.4.2"
        assert body["file_count"] == 1
        assert body["files"][0]["basename"] == "app.min.js.map"

        # It landed inside the project tree at the safe basename, nowhere else.
        stored = sourcemaps.destination_path(
            maps_dir, str(org_id), str(project_id), "web@1.4.2", "app.min.js.map"
        )
        assert os.path.isfile(stored)
        assert not os.path.exists(os.path.join(maps_dir, "evil"))

        # List reflects the upload.
        listed = await client.get(base, headers=admin_h)
        assert listed.status_code == 200
        assert listed.json()[0]["release"] == "web@1.4.2"

        # Delete removes it; a second delete is a 404.
        gone = await client.delete(f"{base}/web@1.4.2", headers=admin_h)
        assert gone.status_code == 204
        assert (await client.delete(f"{base}/web@1.4.2", headers=admin_h)).status_code == 404
        assert (await client.get(base, headers=admin_h)).json() == []
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})
            await conn.execute(
                text("DELETE FROM users WHERE id IN (:a, :m, :o)"),
                {"a": admin_id, "m": member_id, "o": outsider_id},
            )


@pytest.mark.isolation
async def test_cross_org_admin_cannot_touch_other_project(
    superuser_engine, client, maps_dir
) -> None:
    a_admin_email = f"amadmin-{uuid.uuid4()}@example.test"
    b_admin_email = f"bmadmin-{uuid.uuid4()}@example.test"
    async with superuser_engine.begin() as conn:
        org_a = await _seed_org(conn, "OrgA")
        org_b = await _seed_org(conn, "OrgB")
        project_a = await _seed_project(conn, org_a, "web-a")
        a_admin = await _seed_user(conn, a_admin_email)
        b_admin = await _seed_user(conn, b_admin_email)
        await _add_membership(conn, org_a, a_admin, "admin")
        await _add_membership(conn, org_b, b_admin, "admin")
    b_admin_h = {"Authorization": f"Bearer {security.create_access_token(b_admin)}"}
    try:
        # Org B's admin, addressing org B in the path but org A's project id,
        # is refused with 404 on ALL THREE verbs: project_a does not belong to
        # org B, and the ownership check (explicit org predicate, not RLS
        # visibility alone) runs BEFORE any filesystem write or delete.
        cross = f"/orgs/{org_b}/projects/{project_a}/sourcemaps"
        r = await client.post(
            cross,
            data={"release": "web@1.0.0"},
            files={"files": ("app.min.js.map", _map_bytes(), "application/json")},
            headers=b_admin_h,
        )
        assert r.status_code == 404
        assert (await client.get(cross, headers=b_admin_h)).status_code == 404
        assert (
            await client.delete(f"{cross}/web@1.0.0", headers=b_admin_h)
        ).status_code == 404
        # And NOTHING was written ANYWHERE on disk -- not under org A's tree,
        # not under org B's tree keyed by org A's project id (the exact failure
        # mode CI run 28705817594 caught: 201 + files at {org_b}/{project_a}),
        # and no leftover .tmp partials anywhere. The maps root stays untouched.
        assert not os.path.exists(os.path.join(maps_dir, str(org_a)))
        assert not os.path.exists(os.path.join(maps_dir, str(org_b)))
        assert os.listdir(maps_dir) == []
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM orgs WHERE id IN (:a, :b)"), {"a": org_a, "b": org_b}
            )
            await conn.execute(
                text("DELETE FROM users WHERE id IN (:a, :b)"),
                {"a": a_admin, "b": b_admin},
            )


def _js_envelope(event_id: str, release: str) -> dict:
    """A javascript exception event whose single frame points at the minified asset."""
    return {
        "event_id": event_id,
        "timestamp": "2026-07-04T12:00:00.000Z",
        "platform": "javascript",
        "level": "error",
        "environment": "production",
        "release": release,
        "sdk": {"name": "crashlens-browser", "version": "0.1.0"},
        "exception": {
            "type": "Error",
            "value": "Division by zero in invoice total",
            "stacktrace": {
                "frames": [
                    {
                        "filename": "https://cdn.example.com/app.min.js",
                        "function": "t",
                        "lineno": 1,
                        "colno": 42,
                        "in_app": True,
                    }
                ]
            },
        },
    }


async def test_symbolication_end_to_end_fingerprints_on_originals(
    superuser_engine, app_sessionmaker, maps_dir
) -> None:
    import datetime

    from app.db import tenant_session

    org_id = uuid.uuid4()
    project_id = uuid.uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO orgs (id, name, slug) VALUES (:id, :n, :s)"),
            {"id": org_id, "n": "SymCo", "s": f"symco-{org_id}"},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, org_id, name, slug, platform) "
                "VALUES (:id, :oid, :n, :s, 'javascript')"
            ),
            {"id": project_id, "oid": org_id, "n": "web", "s": f"web-{project_id}"},
        )
    release = "web@1.4.2"
    # Store the real fixture map where symbolication will look for it.
    release_dir = sourcemaps.release_maps_dir(
        maps_dir, str(org_id), str(project_id), release
    )
    os.makedirs(release_dir)
    with open(os.path.join(release_dir, "app.min.js.map"), "wb") as handle:
        handle.write(_map_bytes())

    envelope = _js_envelope(str(uuid.uuid4()), release)
    # The fingerprint the worker SHOULD compute is the one over the symbolicated
    # envelope (originals), NOT the minified one.
    symbolicated = sourcemaps.symbolicate_envelope(
        normalize_envelope(envelope), str(org_id), str(project_id), maps_dir
    )
    expected_fp = compute_fingerprint(symbolicated)
    minified_fp = compute_fingerprint(normalize_envelope(envelope))
    assert expected_fp != minified_fp  # symbolication actually changed the frames.

    try:
        result = await process_event(
            {},
            envelope=envelope,
            org_id=str(org_id),
            project_id=str(project_id),
            dsn_key_id=str(uuid.uuid4()),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
            session_factory=app_sessionmaker,
            sourcemaps_dir=maps_dir,
        )
        assert result["created"] is True
        # The Issue exists under the ORIGINAL-frame fingerprint.
        async with tenant_session(str(org_id), session_factory=app_sessionmaker) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM issues "
                        "WHERE project_id = :p AND fingerprint = :f"
                    ),
                    {"p": project_id, "f": expected_fp},
                )
            ).scalar_one()
            assert row == 1
            # The stored event payload carries the symbolicated frame.
            payload = (
                await session.execute(
                    text(
                        "SELECT payload FROM events "
                        "WHERE project_id = :p AND event_id = :e"
                    ),
                    {"p": project_id, "e": envelope["event_id"]},
                )
            ).scalar_one()
        frame = payload["exception"]["stacktrace"]["frames"][0]
        assert frame["filename"] == "../src/invoice.ts"
        assert frame["lineno"] == 4
        assert frame["raw_filename"] == "https://cdn.example.com/app.min.js"
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM events WHERE org_id = :o"), {"o": org_id}
            )
            await conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})
