"""SQLAlchemy 2.0 typed models mirroring the v1 Crashlens schema.

Every model here corresponds one-to-one with a table created by
``alembic/versions/0001_v1_schema.py``. Column types, defaults, constraints, and
names are kept identical to the migration by hand. Autogenerate is intentionally
NOT used (see ``app/models/base.py``).

Tenancy note: every model except :class:`User` carries a tenant scope. :class:`Org`
is the tenant root and is scoped by its own ``id``; all other tenant tables carry
``org_id``. Row Level Security enforces the scope at the database layer, so the
application never writes ``WHERE org_id = ...`` by hand: it opens a
``tenant_session`` (see ``app/db.py``) that sets ``app.current_org`` and lets RLS
filter every statement.
"""

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

_UUID_PK = UUID(as_uuid=True)


class User(Base):
    """Cross-tenant auth identity. No tenant column, no RLS."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    password_hash: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    is_instance_admin: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("false")
    )
    # Account-security columns added by migration 0002 (auth slice). Kept in sync
    # with the migration by hand, like every other column here.
    failed_login_count: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, server_default=sa.text("0")
    )
    locked_until: Mapped[datetime.datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    __table_args__ = (sa.UniqueConstraint("email", name="uq_users_email"),)


class Org(Base):
    """Tenant root. Scoped by its own ``id`` under RLS."""

    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    slug: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    __table_args__ = (sa.UniqueConstraint("slug", name="uq_orgs_slug"),)


class OrgMembership(Base):
    __tablename__ = "org_memberships"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    __table_args__ = (
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_memberships_role"),
        sa.UniqueConstraint("org_id", "user_id", name="uq_memberships_org_user"),
    )


class OrgInvite(Base):
    __tablename__ = "org_invites"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    role: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    token_hash: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    accepted_at: Mapped[datetime.datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    __table_args__ = (
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_invites_role"),
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    slug: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    platform: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    sampling_rate: Mapped[float] = mapped_column(
        sa.Float(), nullable=False, server_default=sa.text("1.0")
    )
    retention_days: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, server_default=sa.text("30")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    __table_args__ = (sa.UniqueConstraint("org_id", "slug", name="uq_projects_org_slug"),)


class DsnKey(Base):
    __tablename__ = "dsn_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    public_key: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.Text(), nullable=False, server_default=sa.text("'active'")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_dsn_keys_status"),
        sa.UniqueConstraint("public_key", name="uq_dsn_keys_public_key"),
    )


class Release(Base):
    __tablename__ = "releases"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    __table_args__ = (
        sa.UniqueConstraint("project_id", "version", name="uq_releases_project_version"),
    )


class Issue(Base):
    __tablename__ = "issues"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    title: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    level: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.Text(), nullable=False, server_default=sa.text("'unresolved'")
    )
    first_seen: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    last_seen: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    event_count: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, server_default=sa.text("0")
    )
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        _UUID_PK, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_in_release: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    __table_args__ = (
        sa.CheckConstraint(
            "level IN ('fatal', 'error', 'warning', 'info', 'debug')",
            name="ck_issues_level",
        ),
        sa.CheckConstraint(
            "status IN ('unresolved', 'resolved', 'ignored', 'regressed')",
            name="ck_issues_status",
        ),
        sa.UniqueConstraint(
            "project_id", "fingerprint", name="uq_issues_project_fingerprint"
        ),
    )


class Event(Base):
    """One ingested event. Physically a daily RANGE partition of ``events``.

    No foreign keys (hot ingest path); the composite primary key
    ``(project_id, event_id, received_at)`` includes the partition key and
    provides idempotency.
    """

    __tablename__ = "events"

    org_id: Mapped[uuid.UUID] = mapped_column(_UUID_PK, nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(_UUID_PK, primary_key=True)
    issue_id: Mapped[uuid.UUID | None] = mapped_column(_UUID_PK, nullable=True)
    event_id: Mapped[uuid.UUID] = mapped_column(_UUID_PK, primary_key=True)
    received_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), primary_key=True, server_default=sa.text("now()")
    )
    environment: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    release: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    level: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB(), nullable=False)

    __table_args__ = (
        sa.CheckConstraint(
            "level IN ('fatal', 'error', 'warning', 'info', 'debug')",
            name="ck_events_level",
        ),
        {"postgresql_partition_by": "RANGE (received_at)"},
    )


class IssueComment(Base):
    __tablename__ = "issue_comments"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    issue_id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=False
    )
    author: Mapped[uuid.UUID | None] = mapped_column(
        _UUID_PK, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=False
    )
    body: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class AlertChannel(Base):
    __tablename__ = "alert_channels"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID_PK, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    config: Mapped[dict] = mapped_column(
        JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("true")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    __table_args__ = (
        sa.CheckConstraint(
            "type IN ('email', 'slack', 'webhook')", name="ck_alert_channels_type"
        ),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        _UUID_PK, sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID_PK, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    target_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    target_id: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    data: Mapped[dict] = mapped_column(
        JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
