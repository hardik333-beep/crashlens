"""auth account-security columns on users

Revision ID: 0002_auth_account_security
Revises: 0001_v1_schema
Create Date: 2026-07-04

Every Crashlens revision MUST implement both upgrade() and downgrade() with
explicit, reversible operations. Do not leave downgrade() as a no-op.

WHAT THIS REVISION DOES
-----------------------
Adds three account-security columns to the RLS-exempt ``users`` table, used by
the login lockout logic in the auth slice:

- ``failed_login_count`` int NOT NULL default 0: consecutive failed logins since
  the last success or lock expiry.
- ``locked_until`` timestamptz NULL: when set to a future time, the account is
  locked; cleared on a successful login.
- ``last_login_at`` timestamptz NULL: stamp of the most recent successful login.

The ``failed_login_count`` default of 0 backfills existing rows in place, so the
NOT NULL add is safe. Nothing else changes: no new tables, indexes, policies, or
grants. ``users`` has no RLS, so no policy work is required.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_auth_account_security"
down_revision: str | None = "0001_v1_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "failed_login_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "users",
        sa.Column("locked_until", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Reverse in the opposite order of addition.
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_count")
