"""Account and organization services: the data-layer half of the auth slice.

Route handlers stay thin and call into these functions; the FastAPI dependencies
in ``app/auth.py`` call the membership and user-load helpers. Every function
accepts an optional ``session_factory`` so integration tests can bind a
NON-superuser engine and exercise the real Row Level Security policies (the
production callers omit it and use the default factory).

SESSION CHOICE PER OPERATION (grounded in app/db.py's docstring):

* ``users`` has no tenant scope and no RLS. A plain ``crashlens_app`` session
  reads and writes it directly (user lookup, lockout counter, user creation).
* Creating an org uses the documented signup pattern: generate the org uuid in
  the application, open ``tenant_session(new_id)``, and INSERT the org, its admin
  membership (and, at signup, the owning user) in ONE transaction. WITH CHECK
  passes because the GUC matches the new rows' scope, so the whole account is
  created atomically or not at all.
* Listing a user's orgs and resolving an invite token are cross-tenant reads
  that happen before org context exists, so they go through ``system_session``
  (read-only, BYPASSRLS, SELECT on the four bootstrap tables only).
* Adding a membership from an accepted invite and marking the invite accepted are
  org-scoped writes, so they run inside ``tenant_session(invite.org_id)`` where
  the org id came from the trusted ``system_session`` token lookup, never client
  input.

SECRETS HYGIENE: no password, hash, or token is ever logged here.
"""

import datetime
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import security
from app.db import get_sessionmaker, system_session, tenant_session
from app.models.schema import User

_VALID_ROLES = ("admin", "member")


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


@asynccontextmanager
async def _plain_txn(
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> AsyncIterator[AsyncSession]:
    """Yield a plain (no GUC, no role) transaction for RLS-exempt ``users`` work.

    Commits on clean exit, rolls back on exception. Used only for the ``users``
    table, which carries no tenant scope.
    """
    factory = session_factory or get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            yield session


def _make_org_slug(org_name: str) -> str:
    """Return a url-safe, collision-resistant slug derived from ``org_name``."""
    base = re.sub(r"[^a-z0-9]+", "-", org_name.lower()).strip("-") or "org"
    return f"{base[:40]}-{security.generate_invite_token()[:8]}"


async def load_user_by_email(
    email: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> User | None:
    """Return the user with ``email`` or None. Plain session (users has no RLS)."""
    factory = session_factory or get_sessionmaker()
    async with factory() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()


async def load_user_by_id(
    user_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> User | None:
    """Return the user with ``user_id`` or None. Plain session (users has no RLS)."""
    factory = session_factory or get_sessionmaker()
    async with factory() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()


async def load_users_by_ids(
    user_ids: list[uuid.UUID],
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[uuid.UUID, str]:
    """Return a ``{user_id: email}`` map for the given ids. Plain session.

    ``users`` carries no tenant scope and no RLS, so a plain ``crashlens_app``
    session reads it directly (same discipline as ``load_user_by_id``). Callers
    that already resolved a set of org members via ``tenant_session`` use this to
    attach emails, which live on the RLS-exempt ``users`` table.
    """
    if not user_ids:
        return {}
    factory = session_factory or get_sessionmaker()
    async with factory() as session:
        rows = (
            await session.execute(select(User.id, User.email).where(User.id.in_(user_ids)))
        ).all()
    return {row.id: row.email for row in rows}


async def list_user_orgs(
    user_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> list[dict]:
    """Return the orgs ``user_id`` belongs to, via the read-only system session.

    This is bootstrap flow (1) from app/db.py: cross-tenant membership listing
    before any org context exists.
    """
    async with system_session(session_factory=session_factory) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT o.id AS id, o.name AS name, o.slug AS slug, "
                    "m.role AS role "
                    "FROM org_memberships m JOIN orgs o ON o.id = m.org_id "
                    "WHERE m.user_id = :uid ORDER BY o.created_at"
                ),
                {"uid": str(user_id)},
            )
        ).all()
    return [
        {"id": str(r.id), "name": r.name, "slug": r.slug, "role": r.role} for r in rows
    ]


async def verify_membership(
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> str | None:
    """Return the user's role in ``org_id``, or None if they are not a member.

    This is the membership-verification duty from app/db.py: the ``org_id`` is
    untrusted client input (a path parameter) until this check confirms the
    session user actually belongs to it.
    """
    async with system_session(session_factory=session_factory) as session:
        role = (
            await session.execute(
                text(
                    "SELECT role FROM org_memberships "
                    "WHERE user_id = :uid AND org_id = :oid"
                ),
                {"uid": str(user_id), "oid": str(org_id)},
            )
        ).scalar_one_or_none()
    return role


async def signup(
    email: str,
    password: str,
    org_name: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict | None:
    """Create a user, their org, and their admin membership atomically.

    Returns the created identifiers on success, or None if the email already
    exists (the caller renders an indistinguishable generic response so signup
    does not leak which emails are registered). The password is hashed
    unconditionally so an existing-email request does the same Argon2 work.
    """
    existing = await load_user_by_email(email, session_factory=session_factory)
    # Hash regardless of existence so timing does not separate the two paths.
    password_hash = security.hash_password(password)
    if existing is not None:
        return None

    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    slug = _make_org_slug(org_name)
    try:
        # ONE transaction, org scope = new org id. users has no RLS so it inserts
        # freely; orgs and org_memberships pass WITH CHECK because their scope
        # equals the GUC. All three rows commit together or none do.
        async with tenant_session(str(org_id), session_factory=session_factory) as session:
            await session.execute(
                text(
                    "INSERT INTO users (id, email, password_hash) "
                    "VALUES (:id, :email, :ph)"
                ),
                {"id": str(user_id), "email": email, "ph": password_hash},
            )
            await session.execute(
                text("INSERT INTO orgs (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": str(org_id), "name": org_name, "slug": slug},
            )
            await session.execute(
                text(
                    "INSERT INTO org_memberships (org_id, user_id, role) "
                    "VALUES (:oid, :uid, 'admin')"
                ),
                {"oid": str(org_id), "uid": str(user_id)},
            )
    except IntegrityError:
        # A concurrent signup won the email (or, vanishingly rarely, the slug).
        # Nothing was committed: fall back to the generic response.
        return None

    return {
        "user_id": user_id,
        "email": email,
        "org_id": org_id,
        "org_name": org_name,
        "slug": slug,
        "role": "admin",
    }


async def authenticate(
    email: str,
    password: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> User | None:
    """Verify credentials and maintain the per-account lockout counter.

    Returns the authenticated user, or None for EVERY failure mode (unknown
    email, wrong password, or a currently locked account) so the caller can
    render one identical 401. All bookkeeping lives here:

    * Unknown email: spend dummy Argon2 time, return None (no row written).
    * Locked (locked_until in the future): spend dummy time, return None; the
      lockout check runs only AFTER the account is identified, so it is
      per-principal, not per-IP.
    * A lock whose window has elapsed resets the counter to zero before this
      attempt is judged.
    * Success: reset the counter, clear the lock, stamp last_login_at.
    * Failure: increment; on reaching LOCKOUT_THRESHOLD, set locked_until.
    """
    now = _utcnow()
    async with _plain_txn(session_factory) as session:
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()

        if user is None:
            security.dummy_verify(password)
            return None

        if user.locked_until is not None and user.locked_until > now:
            # Identified but locked: uniform failure, no counter change.
            security.dummy_verify(password)
            return None

        # A previously expired lock starts this attempt from a clean counter.
        effective_count = user.failed_login_count
        if user.locked_until is not None and user.locked_until <= now:
            effective_count = 0

        if security.verify_password(user.password_hash, password):
            user.failed_login_count = 0
            user.locked_until = None
            user.last_login_at = now
            return user

        new_count = effective_count + 1
        user.failed_login_count = new_count
        if new_count >= security.LOCKOUT_THRESHOLD:
            user.locked_until = now + security.LOCKOUT_DURATION
        else:
            user.locked_until = None
        return None


async def create_invite(
    org_id: uuid.UUID,
    email: str,
    role: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> tuple[dict, str]:
    """Create an org invite and return (invite metadata, raw token once).

    Only the SHA-256 hash of the token is stored. ``org_id`` must already be a
    verified admin context (see require_org_admin); it is used to scope the
    write via tenant_session. Delivering the raw token by email is the alerts
    slice's job, so it is returned to the caller exactly once here.
    """
    if role not in _VALID_ROLES:
        raise ValueError(f"role must be one of {_VALID_ROLES}")

    raw_token = security.generate_invite_token()
    token_hash = security.hash_invite_token(raw_token)
    invite_id = uuid.uuid4()
    expires_at = _utcnow() + security.INVITE_TTL

    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        await session.execute(
            text(
                "INSERT INTO org_invites "
                "(id, org_id, email, role, token_hash, expires_at) "
                "VALUES (:id, :oid, :email, :role, :th, :exp)"
            ),
            {
                "id": str(invite_id),
                "oid": str(org_id),
                "email": email,
                "role": role,
                "th": token_hash,
                "exp": expires_at,
            },
        )

    invite = {
        "id": invite_id,
        "org_id": org_id,
        "email": email,
        "role": role,
        "expires_at": expires_at,
    }
    return invite, raw_token


async def accept_invite(
    token: str,
    email: str,
    password: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict | None:
    """Accept an invite: create or verify the user, grant membership, mark used.

    Returns the resulting user/org/role on success. Returns None for any invalid
    token state (unknown, expired, already accepted, or email mismatch) so the
    caller renders one uniform error. Raises PasswordPolicyError only when a NEW
    user supplies a password that fails policy (that concerns the password, not
    the token, and the caller already holds a valid secret).
    """
    token_hash = security.hash_invite_token(token)
    now = _utcnow()

    # Bootstrap flow (2): resolve the token cross-tenant. The org is unknown
    # until this row is read, and this is a read-only lookup.
    async with system_session(session_factory=session_factory) as session:
        invite = (
            await session.execute(
                text(
                    "SELECT id, org_id, email, role, expires_at, accepted_at "
                    "FROM org_invites WHERE token_hash = :th"
                ),
                {"th": token_hash},
            )
        ).one_or_none()

    if invite is None or invite.accepted_at is not None or invite.expires_at <= now:
        return None
    if invite.email.lower() != email.lower():
        return None

    existing = await load_user_by_email(email, session_factory=session_factory)
    if existing is not None:
        if not security.verify_password(existing.password_hash, password):
            return None
        user_id = existing.id
        create_user = False
        password_hash = None
    else:
        policy_error = security.validate_password(password)
        if policy_error is not None:
            raise security.PasswordPolicyError(policy_error)
        user_id = uuid.uuid4()
        password_hash = security.hash_password(password)
        create_user = True

    org_id = invite.org_id
    role = invite.role

    # Org-scoped writes: user creation (RLS-exempt) plus membership and the
    # accepted-at stamp (org-scoped, visible because the GUC is invite.org_id).
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        if create_user:
            await session.execute(
                text(
                    "INSERT INTO users (id, email, password_hash) "
                    "VALUES (:id, :email, :ph)"
                ),
                {"id": str(user_id), "email": email, "ph": password_hash},
            )
        await session.execute(
            text(
                "INSERT INTO org_memberships (org_id, user_id, role) "
                "VALUES (:oid, :uid, :role) "
                "ON CONFLICT ON CONSTRAINT uq_memberships_org_user DO NOTHING"
            ),
            {"oid": str(org_id), "uid": str(user_id), "role": role},
        )
        await session.execute(
            text(
                "UPDATE org_invites SET accepted_at = :now "
                "WHERE id = :id AND accepted_at IS NULL"
            ),
            {"now": now, "id": str(invite.id)},
        )

    return {"user_id": user_id, "email": email, "org_id": org_id, "role": role}
