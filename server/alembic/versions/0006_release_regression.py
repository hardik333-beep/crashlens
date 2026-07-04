"""issues.regressed_in_release for release-aware regression tracking

Revision ID: 0006_release_regression
Revises: 0005_system_projects_grant
Create Date: 2026-07-04

Every Crashlens revision MUST implement both upgrade() and downgrade() with
explicit, reversible operations. Do not leave downgrade() as a no-op.

WHAT THIS REVISION DOES
-----------------------
Adds one nullable column, ``issues.regressed_in_release`` (text), that records
WHICH release an Issue came back in when a resolved Issue regresses on a new
event. It is the companion to the existing ``issues.resolved_in_release`` (the
release an Issue was marked fixed in, added by revision 0001): together they let
the worker decide whether a new event on a RESOLVED Issue is a genuine
regression (an event from a build STRICTLY NEWER, in first-seen order, than the
fix) or just a late straggler from the already-fixed build, and let the UI show
"Fixed in X" / "Came back in Y".

The column is NULL for every existing Issue and is only ever set when a resolved
Issue flips to ``regressed``; it is cleared when an Issue is reopened or ignored
(see ``app/issues.py: set_issue_status``). Adding a nullable column with no
default is a metadata-only change in PostgreSQL (no table rewrite), so it is safe
to apply online.

DOWNGRADE
---------
Dropping the column is the exact reversal and loses only the recorded
came-back-in release string, which is derivable again from event data if needed.
No data outside this column is touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_release_regression"
down_revision: str | None = "0005_system_projects_grant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no server default -> metadata-only add (no table rewrite).
    op.add_column(
        "issues",
        sa.Column("regressed_in_release", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("issues", "regressed_in_release")
