"""v1 schema, RLS tenant isolation, daily-partitioned events

Revision ID: 0001_v1_schema
Revises:
Create Date: 2026-07-04

Every Crashlens revision MUST implement both upgrade() and downgrade() with
explicit, reversible operations. Do not leave downgrade() as a no-op.

WHAT THIS REVISION DOES
-----------------------
Creates the full v1 Crashlens schema, structural multi-tenant isolation via
PostgreSQL Row Level Security (RLS), a daily RANGE-partitioned ``events`` table,
and two least-privilege application roles.

TENANT ISOLATION MODEL (two-role design)
----------------------------------------
Every table except ``users`` carries a tenant scope and is protected by RLS.
``users`` is the cross-tenant auth identity table and has NO tenant column.

Two NOLOGIN privilege roles are created:

- ``crashlens_app``: FORCE-RLS-bound DML. SELECT / INSERT / UPDATE / DELETE on
  every table, no DDL. All tenant-scoped work runs under it, filtered by the
  ``app.current_org`` GUC policy below.
- ``crashlens_system``: read-only BYPASSRLS bootstrap role. SELECT ONLY on
  exactly four tables: ``orgs``, ``org_memberships``, ``org_invites``,
  ``dsn_keys``. It exists because four product flows are inherently
  cross-tenant reads that happen BEFORE org context exists: login ("list the
  orgs this user belongs to"), invite-token resolution, org-slug routing, and
  the ingest DSN public_key lookup. It has no INSERT / UPDATE / DELETE and no
  grant on any other table, so even a compromised bootstrap path cannot write
  or read tenant event data. The application enters it per transaction with
  ``SET LOCAL ROLE crashlens_system`` (see ``app/db.py: system_session``);
  BYPASSRLS takes effect because PostgreSQL checks row security against
  ``current_user``, and SET ROLE changes ``current_user``.

Each tenant table gets exactly ONE policy covering ALL commands, with BOTH a
USING clause (governs which existing rows are visible to SELECT / UPDATE /
DELETE) and a WITH CHECK clause (governs which new rows INSERT / UPDATE may
write). Using a single policy avoids the PERMISSIVE / OR-widening trap where
multiple permissive policies are combined with OR and quietly broaden access.

The scope predicate is::

    <tenant_col> = current_setting('app.current_org', true)::uuid

``current_setting(..., true)`` returns NULL when the GUC is unset (missing_ok).
``<col> = NULL`` evaluates to NULL, which is NOT true, so a session that has not
set ``app.current_org`` sees zero rows and cannot insert: absence DENIES. This
is verified by the integration tests, not assumed.

RLS is both ENABLEd and FORCEd on every tenant table. FORCE ensures the policy
binds even the table owner (owners are otherwise exempt unless FORCE is set).
Note that a PostgreSQL SUPERUSER always bypasses RLS entirely; the integration
tests therefore connect as a dedicated NON-superuser role, and production must
likewise connect as a non-superuser login role (see below).

TRANSACTION-LOCAL GUC TRAP
--------------------------
The org scope is applied with ``set_config('app.current_org', <id>, true)``.
The trailing ``true`` makes it transaction-local (is_local). A transaction-local
GUC is reset at every COMMIT / ROLLBACK, so it MUST be re-applied at the start of
every transaction. The application's ``tenant_session`` context manager
(``app/db.py``) does exactly this on each transaction it opens.

APPLICATION ROLES
-----------------
Both roles are NOLOGIN privilege bundles: they cannot connect directly. The
deployed database login user (whatever ``DATABASE_URL`` uses) must be a
NON-superuser and must be made a member of BOTH, once, out of band::

    GRANT crashlens_app TO <deployed_login_user>;
    GRANT crashlens_system TO <deployed_login_user>;

Membership in ``crashlens_system`` is what authorizes the per-transaction
``SET LOCAL ROLE crashlens_system``; the bypass is opt-in per transaction and
reverts at COMMIT / ROLLBACK. A plain session (no SET ROLE, no GUC) remains
fully RLS-bound and reads zero tenant rows. The integration tests create a
throwaway login role ``crashlens_test``, grant it both bundles, and exercise
RLS and the bootstrap bypass as a real non-superuser client.

The migration itself is run by the schema owner / a superuser (in CI the
``postgres`` service superuser), which is expected: DDL requires ownership.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_v1_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Event / issue severity levels, per docs/PROTOCOL.md section 3.1.
_LEVELS = ("fatal", "error", "warning", "info", "debug")
_LEVEL_CHECK = "level IN ('fatal', 'error', 'warning', 'info', 'debug')"

# Tenant tables scoped by an ``org_id`` column. ``orgs`` is scoped by its own
# ``id`` (it IS the tenant root) and is handled separately. ``users`` has no
# tenant scope and is intentionally absent.
_ORG_SCOPED_TABLES = (
    "org_memberships",
    "org_invites",
    "projects",
    "dsn_keys",
    "issues",
    "events",
    "releases",
    "issue_comments",
    "alert_channels",
    "audit_log",
)

# Every table the application role may read/write, including the RLS-exempt
# ``users`` identity table and the ``orgs`` root.
_ALL_APP_TABLES = ("users", "orgs", *_ORG_SCOPED_TABLES)

_APP_ROLE = "crashlens_app"
_SYSTEM_ROLE = "crashlens_system"

# The ONLY tables the read-only BYPASSRLS bootstrap role may SELECT. These
# serve the four pre-org-context flows: login membership listing, invite-token
# resolution, org-slug routing, and the ingest DSN public_key lookup.
_SYSTEM_READ_TABLES = ("orgs", "org_memberships", "org_invites", "dsn_keys")


def _enable_rls(table: str, scope_col: str = "org_id") -> None:
    """Enable + force RLS on ``table`` and install the single tenant policy.

    One policy for ALL commands, carrying BOTH USING and WITH CHECK so that
    SELECT/UPDATE/DELETE visibility and INSERT/UPDATE write-eligibility are all
    governed by the same org predicate. A missing ``app.current_org`` GUC yields
    NULL and therefore DENIES.
    """
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING ({scope_col} = current_setting('app.current_org', true)::uuid) "
        f"WITH CHECK ({scope_col} = current_setting('app.current_org', true)::uuid)"
    )


def upgrade() -> None:
    # --- Application roles (idempotent; NOLOGIN privilege bundles) ------------
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crashlens_app') "
        "THEN CREATE ROLE crashlens_app NOLOGIN; END IF; "
        "END $$;"
    )
    # Read-only bootstrap role: BYPASSRLS, SELECT on exactly four tables (the
    # grants are issued after the tables exist, below).
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crashlens_system') "
        "THEN CREATE ROLE crashlens_system NOLOGIN BYPASSRLS; END IF; "
        "END $$;"
    )

    # --- users: cross-tenant auth identity, NO org column, NO RLS ------------
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "is_instance_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    # --- orgs: the tenant root; scoped by its own id -------------------------
    op.create_table(
        "orgs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("slug", name="uq_orgs_slug"),
    )

    # --- org_memberships -----------------------------------------------------
    op.create_table(
        "org_memberships",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name="fk_memberships_org", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_memberships_user", ondelete="CASCADE"
        ),
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_memberships_role"),
        sa.UniqueConstraint("org_id", "user_id", name="uq_memberships_org_user"),
    )

    # --- org_invites ---------------------------------------------------------
    op.create_table(
        "org_invites",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name="fk_invites_org", ondelete="CASCADE"
        ),
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_invites_role"),
    )

    # --- projects ------------------------------------------------------------
    op.create_table(
        "projects",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=True),
        sa.Column(
            "sampling_rate",
            sa.Float(),
            nullable=False,
            server_default=sa.text("1.0"),
        ),
        sa.Column(
            "retention_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("30"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name="fk_projects_org", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("org_id", "slug", name="uq_projects_org_slug"),
    )

    # --- dsn_keys ------------------------------------------------------------
    op.create_table(
        "dsn_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name="fk_dsn_keys_org", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_dsn_keys_project",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_dsn_keys_status"),
        sa.UniqueConstraint("public_key", name="uq_dsn_keys_public_key"),
    )

    # --- releases ------------------------------------------------------------
    op.create_table(
        "releases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name="fk_releases_org", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_releases_project",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("project_id", "version", name="uq_releases_project_version"),
    )

    # --- issues --------------------------------------------------------------
    op.create_table(
        "issues",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'unresolved'"),
        ),
        sa.Column(
            "first_seen",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "event_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("assigned_to", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolved_in_release", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name="fk_issues_org", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_issues_project",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to"],
            ["users.id"],
            name="fk_issues_assigned_to",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(_LEVEL_CHECK, name="ck_issues_level"),
        sa.CheckConstraint(
            "status IN ('unresolved', 'resolved', 'ignored', 'regressed')",
            name="ck_issues_status",
        ),
        sa.UniqueConstraint(
            "project_id", "fingerprint", name="uq_issues_project_fingerprint"
        ),
    )

    # --- events: daily RANGE-partitioned by received_at ----------------------
    # Raw SQL because Alembic op.create_table cannot express PARTITION BY. The
    # PK includes the partition key (received_at), as PostgreSQL requires, and
    # doubles as the (project_id, event_id, received_at) idempotency key.
    # No foreign keys on this hot ingest table (referential integrity is
    # enforced by the application) to keep the write path cheap. See FLAGGED
    # DEFAULTS in the slice report.
    op.execute(
        "CREATE TABLE events ("
        "  org_id uuid NOT NULL,"
        "  project_id uuid NOT NULL,"
        "  issue_id uuid,"
        "  event_id uuid NOT NULL,"
        "  received_at timestamptz NOT NULL DEFAULT now(),"
        "  environment text NOT NULL,"
        "  release text,"
        "  level text NOT NULL,"
        "  payload jsonb NOT NULL,"
        f"  CONSTRAINT ck_events_level CHECK ({_LEVEL_CHECK}),"
        "  CONSTRAINT events_pkey PRIMARY KEY (project_id, event_id, received_at)"
        ") PARTITION BY RANGE (received_at)"
    )
    op.execute(
        "CREATE INDEX ix_events_project_issue_received "
        "ON events (project_id, issue_id, received_at)"
    )
    op.execute("CREATE INDEX ix_events_payload_gin ON events USING gin (payload jsonb_path_ops)")
    op.execute("CREATE INDEX ix_events_org ON events (org_id)")

    # --- issue_comments ------------------------------------------------------
    op.create_table(
        "issue_comments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issue_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name="fk_comments_org", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["issue_id"],
            ["issues.id"],
            name="fk_comments_issue",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author"], ["users.id"], name="fk_comments_author", ondelete="SET NULL"
        ),
    )

    # --- alert_channels (project_id NULL means org-wide) ---------------------
    op.create_table(
        "alert_channels",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name="fk_alert_channels_org", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_alert_channels_project",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "type IN ('email', 'slack', 'webhook')", name="ck_alert_channels_type"
        ),
    )

    # --- audit_log -----------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column(
            "data",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name="fk_audit_log_org", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_audit_log_actor",
            ondelete="SET NULL",
        ),
    )

    # --- Partition management functions --------------------------------------
    # Idempotent daily-partition creator. Naming: events_YYYYMMDD. Mirrored in
    # Python by app.db.events_partition_name for the application.
    op.execute(
        "CREATE OR REPLACE FUNCTION create_events_partition(day date) "
        "RETURNS void LANGUAGE plpgsql AS $$ "
        "DECLARE "
        "  part_name text := 'events_' || to_char(day, 'YYYYMMDD'); "
        "BEGIN "
        "  EXECUTE format("
        "    'CREATE TABLE IF NOT EXISTS %I PARTITION OF events "
        "FOR VALUES FROM (%L) TO (%L)', "
        "    part_name, day::text, (day + 1)::text"
        "  ); "
        "END; $$"
    )
    # Retention helper (used by a later slice). Drops any daily partition whose
    # day is strictly before ``day``, matching the events_YYYYMMDD naming.
    op.execute(
        "CREATE OR REPLACE FUNCTION drop_events_partitions_before(day date) "
        "RETURNS void LANGUAGE plpgsql AS $$ "
        "DECLARE r record; part_day date; "
        "BEGIN "
        "  FOR r IN "
        "    SELECT c.relname AS name "
        "    FROM pg_inherits i "
        "    JOIN pg_class c ON c.oid = i.inhrelid "
        "    JOIN pg_class p ON p.oid = i.inhparent "
        "    WHERE p.relname = 'events' AND c.relname ~ '^events_[0-9]{8}$' "
        "  LOOP "
        "    part_day := to_date(right(r.name, 8), 'YYYYMMDD'); "
        "    IF part_day < day THEN "
        "      EXECUTE format('DROP TABLE IF EXISTS %I', r.name); "
        "    END IF; "
        "  END LOOP; "
        "END; $$"
    )
    # Pre-create partitions for today through today + 7 days so ingest has a
    # home immediately. A later slice schedules ongoing creation.
    for offset in range(8):
        op.execute(f"SELECT create_events_partition((CURRENT_DATE + {offset})::date)")

    # --- Row Level Security on every tenant table ----------------------------
    _enable_rls("orgs", scope_col="id")
    for table in _ORG_SCOPED_TABLES:
        _enable_rls(table)

    # --- Least-privilege grants to the application roles ----------------------
    op.execute(f"GRANT USAGE ON SCHEMA public TO {_APP_ROLE}")
    for table in _ALL_APP_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_APP_ROLE}")

    # Bootstrap role: SELECT ONLY, on exactly the four pre-org-context tables.
    op.execute(f"GRANT USAGE ON SCHEMA public TO {_SYSTEM_ROLE}")
    for table in _SYSTEM_READ_TABLES:
        op.execute(f"GRANT SELECT ON {table} TO {_SYSTEM_ROLE}")


def downgrade() -> None:
    # Reverse in dependency order. Dropping a table drops its policies, indexes,
    # constraints, and (for the partitioned parent) all partitions, so policies
    # need no separate DROP. Grants vanish with the tables.
    op.execute("DROP FUNCTION IF EXISTS drop_events_partitions_before(date)")
    op.execute("DROP FUNCTION IF EXISTS create_events_partition(date)")

    op.drop_table("audit_log")
    op.drop_table("alert_channels")
    op.drop_table("issue_comments")
    op.execute("DROP TABLE IF EXISTS events")
    op.drop_table("issues")
    op.drop_table("releases")
    op.drop_table("dsn_keys")
    op.drop_table("projects")
    op.drop_table("org_invites")
    op.drop_table("org_memberships")
    op.drop_table("orgs")
    op.drop_table("users")

    # Remove both application roles. REVOKE the schema grants first; table
    # grants are already gone with the tables. In production, any login user
    # that was made a member (GRANT crashlens_app / crashlens_system TO <user>)
    # must have those memberships revoked before this can drop the roles; CI
    # grants no such membership.
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {_SYSTEM_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {_SYSTEM_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {_APP_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {_APP_ROLE}")
