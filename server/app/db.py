"""Async database engine, session factories, and tenant-scoped sessions.

The heart of this module is :func:`tenant_session`, the ONLY sanctioned way for
application code to touch tenant data. It sets the ``app.current_org`` GUC at the
start of every transaction so PostgreSQL Row Level Security (RLS) filters every
statement by org. Application code therefore never writes ``WHERE org_id = ...``
by hand: isolation is structural, enforced by the database.

TRANSACTION-LOCAL GUC TRAP
--------------------------
The org scope is set with ``set_config('app.current_org', <id>, true)``. The
trailing ``true`` makes it *transaction-local*: PostgreSQL resets it at every
COMMIT and ROLLBACK. A value set in one transaction is GONE in the next. So the
scope MUST be re-applied at the start of EVERY transaction. :func:`tenant_session`
opens exactly one transaction and applies the scope as its first statement, so a
fresh scope is guaranteed for every unit of work. Never cache a session across
transactions expecting the scope to persist.

READ-ONLY SYSTEM SESSION (BYPASSRLS bootstrap)
----------------------------------------------
:func:`system_session` serves exactly four bootstrap flows that are inherently
cross-tenant reads happening BEFORE org context exists:

1. Login: "list the orgs this user belongs to" (``org_memberships`` by user_id).
2. Invite acceptance: resolving an invite token (``org_invites``; the org is
   unknown until the token row is read).
3. Org routing: resolving an org slug to an id (``orgs``).
4. Ingest (the hot path): looking up a DSN ``public_key`` (``dsn_keys``),
   JOINed to ``projects`` in the same lookup, to learn WHICH project/org an
   event belongs to AND its per-project ``sampling_rate`` -- the sampling
   decision (W6-04) is made before any tenant context exists, so it rides
   along with this bootstrap read rather than requiring a second,
   tenant-scoped query.

It opens a transaction and issues ``SET LOCAL ROLE crashlens_system`` as the
first statement. ``crashlens_system`` is a NOLOGIN role created by migration
0001 (SELECT on ``orgs``, ``org_memberships``, ``org_invites``, ``dsn_keys``)
and extended by migration 0005 (SELECT on ``projects``), with BYPASSRLS and
SELECT-only grants on exactly those FIVE tables. BYPASSRLS takes effect
because PostgreSQL checks row security against ``current_user``, and SET ROLE
changes ``current_user``. ``SET LOCAL`` reverts at COMMIT / ROLLBACK, the same
wipe semantics as the GUC, so the bypass is opt-in per transaction; a plain
session stays fully RLS-bound. The role cannot write anything and cannot read
any other table, so misuse fails loudly with a privilege error. Everything
else goes through :func:`tenant_session`.

(The RLS-free ``users`` table needs no bypass: a plain ``crashlens_app``
session reads it directly, e.g. the user-by-email lookup at login.)

MEMBERSHIP VERIFICATION (authZ, not just isolation)
---------------------------------------------------
RLS scopes data to whatever the GUC says, but NOTHING here verifies the caller
may claim that org. Any code path calling ``tenant_session(org_id)`` MUST have
derived ``org_id`` from the session user's verified membership (or from a
``system_session`` DSN / invite lookup), NEVER from client input. That authZ
check is the auth slice's duty; it is recorded here so it is not lost.

ORG CREATION (signup pattern, no system role needed)
----------------------------------------------------
Creating a new org works WITHOUT the system role: generate the org uuid in the
application, call ``tenant_session(new_id)``, and INSERT the org row with
``id = new_id`` plus the initial admin membership in that same transaction.
WITH CHECK passes because the GUC matches the new row's scope.
"""

import datetime
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

# GUC that carries the current org id for the life of a transaction.
_ORG_GUC = "app.current_org"

# Read-only BYPASSRLS bootstrap role created by migration 0001. The connecting
# login user must be a member (GRANT crashlens_system TO <login user>) for
# SET LOCAL ROLE to succeed.
_SYSTEM_ROLE = "crashlens_system"

# Read-only BYPASSRLS operator role created by migration 0007, used ONLY by the
# instance-admin panel for cross-tenant stats. Same membership requirement as
# the bootstrap role (GRANT crashlens_admin TO <login user>).
_ADMIN_ROLE = "crashlens_admin"


def events_partition_name(day: datetime.date) -> str:
    """Return the physical partition table name for ``events`` on ``day``.

    Mirrors the SQL ``create_events_partition`` function's naming
    (``events_YYYYMMDD``) so application code and the database agree without a
    round trip. Pure function; safe to call without a database.
    """
    return f"events_{day.strftime('%Y%m%d')}"


@lru_cache
def get_engine():
    """Return a cached async engine bound to the configured DATABASE_URL."""
    settings = get_settings()
    return create_async_engine(settings.database_url, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return a cached session factory bound to the default engine."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)


@asynccontextmanager
async def tenant_session(
    org_id: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[AsyncSession]:
    """Yield a session inside a transaction scoped to ``org_id`` via RLS.

    The org scope is applied as the transaction's first statement using a
    transaction-local GUC, so every query in the block is filtered by RLS. The
    transaction commits on clean exit and rolls back on exception.

    ``session_factory`` is injectable for tests (to bind a non-superuser engine);
    production code omits it and uses the default factory.
    """
    factory = session_factory or get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            # Re-applied on THIS transaction; a prior transaction's value is
            # already gone (transaction-local GUC, wiped by commit/rollback).
            await session.execute(
                text(f"SELECT set_config('{_ORG_GUC}', :org_id, true)"),
                {"org_id": str(org_id)},
            )
            yield session


@asynccontextmanager
async def system_session(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[AsyncSession]:
    """Yield a read-only cross-tenant session for the four bootstrap flows ONLY.

    Sanctioned uses (see module docstring): login membership listing,
    invite-token resolution, org-slug routing, and the ingest DSN public_key
    lookup. Everything else goes through :func:`tenant_session`.

    Opens a transaction and issues ``SET LOCAL ROLE crashlens_system`` as its
    first statement. The role has BYPASSRLS (row security is checked against
    ``current_user``, which SET ROLE changes) and SELECT-only grants on exactly
    ``orgs``, ``org_memberships``, ``org_invites``, and ``dsn_keys``: writes and
    any other table raise a privilege error. ``SET LOCAL`` reverts at
    COMMIT / ROLLBACK (the same wipe semantics as the transaction-local GUC),
    so the bypass never outlives the transaction.
    """
    factory = session_factory or get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            # Role name is a compile-time constant, not user input; SET ROLE
            # takes no bind parameters.
            await session.execute(text(f"SET LOCAL ROLE {_SYSTEM_ROLE}"))
            yield session


@asynccontextmanager
async def admin_session(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[AsyncSession]:
    """Yield a READ-ONLY cross-tenant session for the instance-admin panel ONLY.

    This backs the instance-operator views (W6-03): whole-instance counts,
    recent event volume, per-org rollups, and partition stats. Those reads span
    every tenant, so they cannot use :func:`tenant_session` (RLS-bound to one
    org) and must not hand-write org filters. They also cannot use
    :func:`system_session`, whose bootstrap role is granted SELECT on only five
    tables on purpose.

    Opens a transaction and issues ``SET LOCAL ROLE crashlens_admin`` as its
    first statement. ``crashlens_admin`` (migration 0007) has BYPASSRLS and
    SELECT-only grants on exactly the tables the panel reads (users, orgs,
    org_memberships, projects, issues, events, releases, alert_channels,
    audit_log): any write raises a privilege error, and any unlisted table is
    unreadable. ``SET LOCAL`` reverts at COMMIT / ROLLBACK, so the bypass never
    outlives the transaction and a plain session stays fully RLS-bound.

    SAFETY: this is gated at the route layer by ``require_instance_admin`` (a
    403 for non-instance-admins) and is NEVER used in a tenant request path.
    Tenant data access always goes through :func:`tenant_session`.
    """
    factory = session_factory or get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            # Role name is a compile-time constant, not user input.
            await session.execute(text(f"SET LOCAL ROLE {_ADMIN_ROLE}"))
            yield session
