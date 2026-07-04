"""instance-admin read-only role for the operator panel (crashlens_admin)

Revision ID: 0007_admin_role
Revises: 0006_release_regression
Create Date: 2026-07-04

Every Crashlens revision MUST implement both upgrade() and downgrade() with
explicit, reversible operations. Do not leave downgrade() as a no-op.

WHAT THIS REVISION DOES
-----------------------
The instance-admin panel (W6-03) shows a self-hoster cross-tenant OPERATOR
stats: how many users / orgs / projects / issues exist across the whole
instance, recent event volume, and the physical event partitions. Those reads
span every tenant, so they cannot use ``crashlens_app`` (RLS-bound to one org
via the ``app.current_org`` GUC) and cannot use ``crashlens_system`` (the
bootstrap role is granted SELECT on only five tables: orgs, org_memberships,
org_invites, dsn_keys, projects -- not issues/events/etc, on purpose).

Rather than widen ``crashlens_system`` (whose narrow allowlist is a security
property of the ingest/login bootstrap path) or hand-write org filters (banned:
isolation is structural), this revision adds a THIRD least-privilege role
dedicated to the operator panel::

    CREATE ROLE crashlens_admin NOLOGIN BYPASSRLS;

It is READ-ONLY: it is granted SELECT (never INSERT / UPDATE / DELETE) on
exactly the tables the overview/orgs/users views read::

    users, orgs, org_memberships, projects, issues, events, releases,
    alert_channels, audit_log

BYPASSRLS lets it read every tenant's rows in one query (PostgreSQL checks row
security against ``current_user``, and the application enters the role per
transaction with ``SET LOCAL ROLE crashlens_admin``; see
``app/db.py: admin_session``). Because the grants are SELECT-only, even this
BYPASSRLS role cannot mutate anything: the single admin write in the whole
panel (toggling ``users.is_instance_admin``) deliberately runs on the normal
``crashlens_app`` role against the RLS-exempt ``users`` table, NOT through
``crashlens_admin``. The route layer additionally gates every admin endpoint
behind ``require_instance_admin`` (a 403 for non-admins), so the role is never
reachable from a tenant request path.

OUT-OF-BAND MEMBERSHIP (mirrors crashlens_system in 0001)
---------------------------------------------------------
Like ``crashlens_app`` and ``crashlens_system``, this is a NOLOGIN privilege
bundle: it cannot connect directly. The deployed database login user must be
made a member ONCE, out of band, exactly as migration 0001 documents for the
other two roles::

    GRANT crashlens_admin TO <deployed_login_user>;

Membership is what authorizes the per-transaction ``SET LOCAL ROLE
crashlens_admin``; the bypass is opt-in per transaction and reverts at
COMMIT / ROLLBACK. The integration tests grant it to the throwaway
``crashlens_test`` login role the same way they grant the other two bundles.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_admin_role"
down_revision: str | None = "0006_release_regression"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADMIN_ROLE = "crashlens_admin"

# The tables the read-only operator panel reads across every tenant. SELECT
# only; no write grant is ever added to this role.
_ADMIN_READ_TABLES = (
    "users",
    "orgs",
    "org_memberships",
    "projects",
    "issues",
    "events",
    "releases",
    "alert_channels",
    "audit_log",
)


def upgrade() -> None:
    # Read-only, BYPASSRLS operator role. Idempotent create so a re-run (or a
    # role left over from a prior downgrade/upgrade cycle) does not error.
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crashlens_admin') "
        "THEN CREATE ROLE crashlens_admin NOLOGIN BYPASSRLS; END IF; "
        "END $$;"
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {_ADMIN_ROLE}")
    for table in _ADMIN_READ_TABLES:
        # SELECT on the partitioned parent ``events`` is checked when querying
        # through the parent, so no per-partition grant is needed.
        op.execute(f"GRANT SELECT ON {table} TO {_ADMIN_ROLE}")


def downgrade() -> None:
    # Table grants vanish with the REVOKE; the schema-usage grant is revoked
    # before the role is dropped. In production any login user made a member
    # (GRANT crashlens_admin TO <user>) must have that membership revoked first;
    # CI's downgrade job runs on a fresh database with no such membership.
    for table in _ADMIN_READ_TABLES:
        op.execute(f"REVOKE SELECT ON {table} FROM {_ADMIN_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {_ADMIN_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {_ADMIN_ROLE}")
