"""grant crashlens_system SELECT on projects for ingest-time sampling lookup

Revision ID: 0005_system_projects_grant
Revises: 0004_comment_author_nullable
Create Date: 2026-07-04

Every Crashlens revision MUST implement both upgrade() and downgrade() with
explicit, reversible operations. Do not leave downgrade() as a no-op.

WHAT THIS REVISION DOES
-----------------------
Per-project event sampling (W6-04) is enforced on the ingest hot path, and the
sampling decision needs the project's ``sampling_rate`` (``projects``, added by
migration 0001) at the exact moment the DSN public key is resolved -- BEFORE any
tenant context exists, in the same ``system_session`` (BYPASSRLS) lookup that
already reads ``dsn_keys`` to learn which project/org an event belongs to.
Requiring a second, tenant-scoped query after that point would mean either
deferring the sampling decision past the point docs/PROTOCOL.md wants it
(before the body is even read) or opening tenant context just to read one
config column. Extending the existing bootstrap lookup with a JOIN is the only
option that keeps the decision on the pre-tenant-context hot path, so this
revision grants the bootstrap role SELECT on exactly one more table::

    GRANT SELECT ON projects TO crashlens_system;

The bootstrap role stays READ-ONLY: no INSERT / UPDATE / DELETE grant is added,
so it still cannot write anything and still cannot read any table beyond its
now-five-table allowlist (``orgs``, ``org_memberships``, ``org_invites``,
``dsn_keys``, ``projects``). ``crashlens_system`` remains BYPASSRLS-scoped to a
single per-transaction ``SET LOCAL ROLE`` (see ``app/db.py: system_session``);
this revision does not touch that mechanism, only its table allowlist.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_system_projects_grant"
down_revision: str | None = "0004_comment_author_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SYSTEM_ROLE = "crashlens_system"


def upgrade() -> None:
    # The bootstrap role gains exactly one more read-only grant: SELECT on
    # projects, so the ingest DSN lookup can JOIN to it for sampling_rate
    # without opening a second, tenant-scoped query.
    op.execute(f"GRANT SELECT ON projects TO {_SYSTEM_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT ON projects FROM {_SYSTEM_ROLE}")
