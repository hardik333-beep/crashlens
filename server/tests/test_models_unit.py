"""Unit tests that need no database.

Cover ORM metadata sanity (the models mirror the migration's shape) and the pure
partition-name computation.
"""

import datetime

from app.db import events_partition_name
from app.models import Base
from app.models.schema import Event, Org, User

_EXPECTED_TABLES = {
    "users",
    "orgs",
    "org_memberships",
    "org_invites",
    "projects",
    "dsn_keys",
    "releases",
    "issues",
    "events",
    "issue_comments",
    "alert_channels",
    "audit_log",
}

# Tenant tables scoped by an ``org_id`` column (everything except users and the
# orgs root, which is scoped by its own id).
_ORG_SCOPED_TABLES = _EXPECTED_TABLES - {"users", "orgs"}


def test_all_expected_tables_are_mapped() -> None:
    assert _EXPECTED_TABLES <= set(Base.metadata.tables)


def test_org_scoped_tables_carry_org_id() -> None:
    for name in _ORG_SCOPED_TABLES:
        table = Base.metadata.tables[name]
        assert "org_id" in table.columns, f"{name} is missing org_id"


def test_users_has_no_tenant_column() -> None:
    # users is the cross-tenant auth identity; it must not carry a tenant scope.
    assert "org_id" not in User.__table__.columns
    assert "org_id" not in Base.metadata.tables["users"].columns


def test_orgs_is_scoped_by_its_own_id() -> None:
    # The tenant root has no org_id; it is scoped by id under RLS.
    assert "org_id" not in Org.__table__.columns
    assert "id" in Org.__table__.columns


def test_events_primary_key_includes_partition_key() -> None:
    pk_cols = {c.name for c in Event.__table__.primary_key.columns}
    assert pk_cols == {"project_id", "event_id", "received_at"}


def test_events_declares_range_partitioning() -> None:
    assert Event.__table__.dialect_options["postgresql"]["partition_by"] == (
        "RANGE (received_at)"
    )


def test_events_partition_name_matches_sql_convention() -> None:
    assert events_partition_name(datetime.date(2026, 7, 4)) == "events_20260704"
    assert events_partition_name(datetime.date(2026, 12, 1)) == "events_20261201"
