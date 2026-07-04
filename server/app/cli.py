"""Operator CLI escape hatches, run from the box with ``python -m app.cli``.

USAGE
-----
Promote a user to instance administrator (recovery path when a self-hoster is
locked out of the operator panel -- e.g. the auto-granted first admin left, or
the last admin's flag was removed some other way)::

    python -m app.cli make-admin someone@example.com

This is the CLI counterpart to the panel's instance-admin toggle. It runs
against a plain (short-lived) async engine built from ``DATABASE_URL`` -- NOT
the pooled application engine and NOT ``admin_session`` (that role is read-only
by design) -- and flips ``users.is_instance_admin`` to true on the RLS-exempt
``users`` table. It never DEMOTES anyone, so it cannot be used to lock the
instance out; demotion (with the last-admin guard) lives only in the panel.

Exit codes: 0 on success, 1 on a usage error or unknown email.
"""

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings

_USAGE = "usage: python -m app.cli make-admin <email>"


async def make_admin(email: str) -> int:
    """Set ``is_instance_admin = true`` for the user with ``email``.

    Returns a process exit code: 0 if a row was updated, 1 if no such user.
    Uses a dedicated engine disposed before returning so the CLI does not leak
    a pool.
    """
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "UPDATE users SET is_instance_admin = true "
                        "WHERE email = :email RETURNING id, email"
                    ),
                    {"email": email},
                )
            ).one_or_none()
    finally:
        await engine.dispose()

    if row is None:
        print(f"No user found with email {email!r}.", file=sys.stderr)
        return 1
    print(f"{row.email} is now an instance administrator.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse argv and dispatch. Kept tiny and dependency-free (no argparse)."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == "make-admin":
        return asyncio.run(make_admin(args[1]))
    print(_USAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
