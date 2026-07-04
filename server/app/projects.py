"""Project and DSN-key services: the data-layer half of the projects slice.

Route handlers in ``app/routes/projects.py`` stay thin and call into these
functions. Like ``app/accounts.py`` every function accepts an optional
``session_factory`` so integration tests can bind a NON-superuser engine and
exercise the real Row Level Security policies (production callers omit it).

SESSION CHOICE PER OPERATION (grounded in app/db.py's docstring):

* All project and DSN-key work is org-scoped and goes through
  ``tenant_session(org_id)``. The ``org_id`` is always the VERIFIED id from an
  ``OrgContext`` (require_org_member / require_org_admin proved the caller's
  membership), never client input. RLS then filters every statement by that org,
  so no handler writes ``WHERE org_id = ...`` by hand.
* Listing members reads ``org_memberships`` inside ``tenant_session`` (org
  scoped), then attaches emails from the RLS-exempt ``users`` table via
  ``accounts.load_users_by_ids`` (a plain session), following the same
  session-choice discipline as the auth slice.

SECRETS HYGIENE: a DSN public key is a NON-secret public identifier (see
docs/PROTOCOL.md) and is intentionally stored and returned in plaintext. No
password, hash, or invite token is handled here.
"""

import datetime
import re
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import accounts, security
from app.db import tenant_session


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _make_project_slug(name: str) -> str:
    """Return a url-safe, collision-resistant slug derived from ``name``.

    Adapted from ``accounts._make_org_slug``: a normalized prefix plus a short
    random suffix, which keeps the per-org slug unique without a retry loop.
    """
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
    return f"{base[:40]}-{security.generate_public_key()[:8]}"


async def _load_project_row(
    session: AsyncSession, project_id: uuid.UUID
) -> object | None:
    """Return the project row visible in the current tenant session, or None.

    RLS on the open ``tenant_session`` already scopes visibility to the org, so
    a project belonging to another org is simply not found (yielding a 404, not
    a cross-tenant read).
    """
    return (
        await session.execute(
            text(
                "SELECT id, name, slug, platform, sampling_rate, created_at "
                "FROM projects WHERE id = :pid"
            ),
            {"pid": str(project_id)},
        )
    ).one_or_none()


def _project_dict(row: object) -> dict:
    return {
        "id": row.id,  # type: ignore[attr-defined]
        "name": row.name,  # type: ignore[attr-defined]
        "slug": row.slug,  # type: ignore[attr-defined]
        "platform": row.platform,  # type: ignore[attr-defined]
        "sampling_rate": row.sampling_rate,  # type: ignore[attr-defined]
        "created_at": row.created_at,  # type: ignore[attr-defined]
    }


async def list_projects(
    org_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> list[dict]:
    """Return the org's projects, newest first, scoped by RLS to ``org_id``."""
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, name, slug, platform, sampling_rate, created_at "
                    "FROM projects ORDER BY created_at DESC"
                )
            )
        ).all()
    return [_project_dict(row) for row in rows]


async def create_project(
    org_id: uuid.UUID,
    name: str,
    platform: str | None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict | None:
    """Create a project in ``org_id`` and return it, or None on a slug clash.

    The project uuid and slug are generated in the application; the INSERT runs
    inside ``tenant_session(org_id)`` so WITH CHECK passes (the row's org scope
    equals the GUC). A duplicate slug (vanishingly rare given the random suffix)
    surfaces as None so the caller can render a uniform conflict response.
    """
    project_id = uuid.uuid4()
    slug = _make_project_slug(name)
    try:
        async with tenant_session(
            str(org_id), session_factory=session_factory
        ) as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO projects (id, org_id, name, slug, platform) "
                        "VALUES (:id, :oid, :name, :slug, :platform) "
                        "RETURNING id, name, slug, platform, sampling_rate, created_at"
                    ),
                    {
                        "id": str(project_id),
                        "oid": str(org_id),
                        "name": name,
                        "slug": slug,
                        "platform": platform,
                    },
                )
            ).one()
    except IntegrityError:
        return None
    return _project_dict(row)


async def update_project_sampling(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    sampling_rate: float,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict | None:
    """Update a project's ``sampling_rate`` and return the refreshed project.

    Returns None if the project is not visible in this org: RLS scopes the
    UPDATE, so a project in another org affects zero rows and the caller
    reports a 404, not a cross-tenant write. Bounds validation (0..1) is the
    route layer's job, done BEFORE this is called, so this function trusts its
    input the same way every other service function does.
    """
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        row = (
            await session.execute(
                text(
                    "UPDATE projects SET sampling_rate = :rate WHERE id = :pid "
                    "RETURNING id, name, slug, platform, sampling_rate, created_at"
                ),
                {"rate": sampling_rate, "pid": str(project_id)},
            )
        ).one_or_none()
    if row is None:
        return None
    return _project_dict(row)


async def get_project(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict | None:
    """Return a project with its ACTIVE DSN keys, or None if not in this org.

    Revoked keys are excluded: the detail view is about keys that can currently
    receive events. RLS scopes both reads to ``org_id``.
    """
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        project = await _load_project_row(session, project_id)
        if project is None:
            return None
        key_rows = (
            await session.execute(
                text(
                    "SELECT id, public_key, status, created_at FROM dsn_keys "
                    "WHERE project_id = :pid AND status = 'active' "
                    "ORDER BY created_at DESC"
                ),
                {"pid": str(project_id)},
            )
        ).all()
    detail = _project_dict(project)
    detail["keys"] = [
        {
            "id": r.id,
            "public_key": r.public_key,
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in key_rows
    ]
    return detail


async def delete_project(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> bool:
    """Delete a project (its DSN keys cascade). Return True if one was deleted.

    RLS scopes the DELETE to ``org_id``, so a project in another org is not
    visible and the delete affects zero rows (reported as a 404, not a
    cross-tenant delete).
    """
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        result = await session.execute(
            text("DELETE FROM projects WHERE id = :pid"),
            {"pid": str(project_id)},
        )
    return result.rowcount > 0


async def create_dsn_key(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict | None:
    """Create an active DSN key for a project, or None if the project is absent.

    The project is confirmed to exist in this org (under RLS) before the key is
    written, so a key can never be minted for a non-existent or other-org
    project. The generated ``public_key`` is stored and returned in plaintext:
    it is a public identifier, not a secret (see docs/PROTOCOL.md).
    """
    key_id = uuid.uuid4()
    public_key = security.generate_public_key()
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        project = await _load_project_row(session, project_id)
        if project is None:
            return None
        row = (
            await session.execute(
                text(
                    "INSERT INTO dsn_keys (id, org_id, project_id, public_key) "
                    "VALUES (:id, :oid, :pid, :pk) "
                    "RETURNING id, public_key, status, created_at"
                ),
                {
                    "id": str(key_id),
                    "oid": str(org_id),
                    "pid": str(project_id),
                    "pk": public_key,
                },
            )
        ).one()
    return {
        "id": row.id,
        "public_key": row.public_key,
        "status": row.status,
        "created_at": row.created_at,
    }


async def revoke_dsn_key(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    key_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> bool:
    """Revoke an active DSN key. Return True if one was revoked.

    Scoped by RLS to ``org_id`` and additionally matched on ``project_id`` and
    the active status, so re-revoking an already-revoked key (or a key from a
    different project) affects zero rows.
    """
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        result = await session.execute(
            text(
                "UPDATE dsn_keys SET status = 'revoked', revoked_at = :now "
                "WHERE id = :kid AND project_id = :pid AND status = 'active'"
            ),
            {"now": _utcnow(), "kid": str(key_id), "pid": str(project_id)},
        )
    return result.rowcount > 0


async def list_members(
    org_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> list[dict]:
    """Return the org's members as ``{user_id, email, role}``, newest first.

    Memberships are read inside ``tenant_session`` (org scoped by RLS); emails
    live on the RLS-exempt ``users`` table and are attached via a plain session
    lookup, per the auth slice's session-choice discipline.
    """
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT user_id, role, created_at FROM org_memberships "
                    "ORDER BY created_at ASC"
                )
            )
        ).all()
    user_ids = [row.user_id for row in rows]
    emails = await accounts.load_users_by_ids(user_ids, session_factory=session_factory)
    return [
        {
            "user_id": row.user_id,
            "email": emails.get(row.user_id, ""),
            "role": row.role,
        }
        for row in rows
    ]
