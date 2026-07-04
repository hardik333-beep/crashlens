"""partition functions SECURITY DEFINER with pinned search_path

Revision ID: 0003_partition_fn_security_definer
Revises: 0002_auth_account_security
Create Date: 2026-07-04

Every Crashlens revision MUST implement both upgrade() and downgrade() with
explicit, reversible operations. Do not leave downgrade() as a no-op.

WHAT THIS REVISION DOES
-----------------------
Closes the privilege gap flagged by slice W2-04 and decided by the governor on
2026-07-04: the partition-maintenance SQL functions ``create_events_partition``
and ``drop_events_partitions_before`` (created by revision 0001) were
SECURITY INVOKER, but attaching/detaching a partition of ``events`` requires
OWNING the parent table. The worker's deployed login role is a non-superuser
member of ``crashlens_app`` (SELECT/INSERT/UPDATE/DELETE only, no ownership),
so in production both retention cron jobs would fail with a "must be owner of
relation events" privilege error. Fix, in two coupled steps:

1. SECURITY DEFINER + PINNED search_path. Both functions are altered to
   ``SECURITY DEFINER`` so the partition DDL inside runs with the function
   OWNER's privileges (the migration/schema-owner role, which owns ``events``)
   instead of the caller's. The pinned ``SET search_path = public, pg_temp``
   is MANDATORY on any SECURITY DEFINER function: without it, name resolution
   inside the function body follows the CALLER's search_path, so a malicious
   caller can put a schema they control (or pg_temp objects) ahead of public
   and have their own same-named tables/functions/operators resolved and
   executed WITH THE DEFINER'S PRIVILEGES - a textbook privilege-escalation
   vector. Pinning to ``public, pg_temp`` (pg_temp last, so temp objects can
   never shadow public ones) removes the caller's influence entirely.

2. EXECUTE REVOKED FROM PUBLIC, GRANTED ONLY TO crashlens_app. PostgreSQL
   grants EXECUTE on every new function to PUBLIC by default, and revision
   0001 never revoked it. That default was tolerable while the functions were
   SECURITY INVOKER (a caller without ownership of ``events`` just got a
   privilege error), but combined with SECURITY DEFINER it would let ANY
   connected role run partition DDL as the owner - e.g. drop every events
   partition. The REVOKE ... FROM PUBLIC plus a narrow
   GRANT EXECUTE ... TO crashlens_app closes that: only the application role
   the worker actually runs under may call these functions.

DOWNGRADE restores the exact post-0001 state: SECURITY INVOKER,
``RESET search_path`` (removing the pinned setting), and
GRANT EXECUTE ... TO PUBLIC (the PostgreSQL default the functions had after
0001, which never touched function grants).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_partition_fn_security_definer"
down_revision: str | None = "0002_auth_account_security"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Exact signatures of the two functions created by revision 0001.
_FUNCTIONS = (
    "create_events_partition(date)",
    "drop_events_partitions_before(date)",
)

_APP_ROLE = "crashlens_app"


def upgrade() -> None:
    for fn in _FUNCTIONS:
        # SECURITY DEFINER must never ship without a pinned search_path (see
        # module docstring point 1): both settings are applied in one ALTER so
        # no intermediate state exists where the definer runs with a
        # caller-controlled search_path.
        op.execute(f"ALTER FUNCTION {fn} SECURITY DEFINER SET search_path = public, pg_temp")
        # Close the PUBLIC-execute + SECURITY DEFINER combination (see module
        # docstring point 2): only the application role may call these.
        op.execute(f"REVOKE EXECUTE ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {_APP_ROLE}")


def downgrade() -> None:
    for fn in _FUNCTIONS:
        # Restore the post-0001 state: invoker security, no pinned
        # search_path, and the PostgreSQL-default PUBLIC execute grant.
        op.execute(f"REVOKE EXECUTE ON FUNCTION {fn} FROM {_APP_ROLE}")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO PUBLIC")
        op.execute(f"ALTER FUNCTION {fn} SECURITY INVOKER RESET search_path")
