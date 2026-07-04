"""Audit log recording: a durable trail of sensitive org-scoped actions.

DESIGN INVARIANT (read this before calling ``record``)
--------------------------------------------------------------------------------
``record`` takes the CALLER'S ALREADY-OPEN session -- the SAME transaction that
performs the audited action -- and issues one more INSERT into it. It never opens
its own ``tenant_session``. This means the audited action and its audit row
share one atomic unit of work: both commit together, or an exception anywhere in
that transaction rolls both back together. There is no separate audit
transaction, no fire-and-forget, and no "best effort" write. A code path that
writes the action but not its audit row (or vice versa) is a bug in the CALLER
(it opened a second transaction, or called ``record`` outside the ``async with
tenant_session(...)`` block), not an accepted edge case of this module.

Callers therefore call ``record`` from INSIDE their own
``async with tenant_session(org_id) as session:`` block, passing that same
``session`` object, after the mutating statement(s) have run (so the audit row
is written only once the outcome -- e.g. "was a row actually deleted?" -- is
known) but BEFORE the block exits (so it is still the same transaction).

ACTION NAMING
-------------
Actions use dot notation, ``"<noun>.<verb>"`` (e.g. ``"project.created"``).
:data:`ACTIONS` is the canonical, closed set this slice supports; ACTION_LABELS
maps each to a short plain-language phrase for the dashboard's Activity view
(the TypeScript mirror in ``dashboard/src/pages/SettingsPage.tsx`` MUST be kept
in sync with this dict -- a unit test checks completeness on the Python side,
but the two are not automatically linked, so keep them consistent by hand).

DATA HYGIENE (the ``data`` guard)
----------------------------------
``data`` holds ONLY small identifying facts about the action -- names, versions,
booleans, counts, and ALREADY-MASKED targets (e.g. ``alerts.mask_target``'s
output). It must NEVER hold a secret: a raw token, password, or an unmasked
URL (a Slack/webhook URL can embed a bearer token in its path or query).
:func:`record` enforces this structurally: any dict key (at any nesting depth)
whose lowercased name contains "token", "secret", "password", or "url" raises
``ValueError`` and the INSERT (and therefore the whole transaction) never
happens. There is no allowlist override in this slice -- if a fact is legitimately
safe to display (e.g. a masked webhook host), store it under a differently named
key (``"target"``, not ``"webhook_url"``) rather than exempting the key name.
"""

import json
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# --- Canonical action set + plain-language labels ------------------------------
# "member.role" (changing an existing member's role) is intentionally absent:
# no service function performs that action yet (only invite-time role
# assignment exists), so there is nothing to instrument. Add it here (and to the
# dashboard's mirror) only when that capability ships.
ACTIONS: tuple[str, ...] = (
    "project.created",
    "project.deleted",
    "key.created",
    "key.revoked",
    "member.invited",
    "invite.accepted",
    "channel.created",
    "channel.updated",
    "channel.deleted",
    "issue.resolved",
    "issue.ignored",
    "issue.reopened",
    "issue.deleted",
    "issue.assigned",
    "sampling.updated",
)

# Short, plain-language, present-tense-agnostic phrases: the dashboard renders
# "<actor> <phrase>" (e.g. "jane@example.com created project"). Keep these free
# of jargon and internal identifiers (Tier 1 plain-language rule).
ACTION_LABELS: dict[str, str] = {
    "project.created": "created project",
    "project.deleted": "deleted project",
    "key.created": "created a DSN key",
    "key.revoked": "revoked a DSN key",
    "member.invited": "invited a teammate",
    "invite.accepted": "accepted an invite",
    "channel.created": "created an alert",
    "channel.updated": "updated an alert",
    "channel.deleted": "removed an alert",
    "issue.resolved": "resolved an error",
    "issue.ignored": "ignored an error",
    "issue.reopened": "reopened an error",
    "issue.deleted": "deleted an error",
    "issue.assigned": "assigned an error",
    "sampling.updated": "updated sampling",
}

# Pagination defaults for the read API (app/routes/audit.py).
DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 100

# Key-name substrings that mark a fact as secret-shaped and therefore forbidden
# in ``data``, regardless of nesting depth. Checked case-insensitively against
# the KEY, never the value (a value can legitimately be an arbitrary string; the
# guard is about what we NAME a field, since callers should never be naming a
# field "webhook_url" or "token" in the first place).
_FORBIDDEN_KEY_SUBSTRINGS = ("token", "secret", "password", "url")


class UnsafeAuditDataError(ValueError):
    """Raised when ``data`` passed to :func:`record` contains a secret-shaped key."""


def _check_data_safety(data: Mapping[str, Any], *, _path: str = "") -> None:
    """Raise :class:`UnsafeAuditDataError` if any key (any depth) looks like a secret."""
    for key, value in data.items():
        lowered = str(key).lower()
        full_path = f"{_path}.{key}" if _path else str(key)
        if any(substring in lowered for substring in _FORBIDDEN_KEY_SUBSTRINGS):
            raise UnsafeAuditDataError(
                f"Audit data key {full_path!r} looks like it holds a secret "
                "(token/secret/password/url) and must not be recorded. Store a "
                "masked or renamed fact instead."
            )
        if isinstance(value, Mapping):
            _check_data_safety(value, _path=full_path)


def clamp_page(value: int | None) -> int:
    """Return a 1-based page number, flooring anything below 1 to 1."""
    if value is None or value < 1:
        return 1
    return int(value)


def clamp_per_page(value: int | None) -> int:
    """Return a per-page size clamped to ``[1, MAX_PER_PAGE]``."""
    if value is None or value < 1:
        return DEFAULT_PER_PAGE
    return min(int(value), MAX_PER_PAGE)


async def record(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | str,
    actor_user_id: uuid.UUID | str | None,
    action: str,
    target_type: str,
    target_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """Insert one ``audit_log`` row on the CALLER'S OPEN ``session``.

    See the module docstring for the transaction-sharing invariant this
    depends on: ``session`` MUST already be inside an open ``tenant_session``
    scoped to ``org_id`` (the RLS ``WITH CHECK`` on ``audit_log`` requires it),
    and this call must happen before that block's ``async with`` exits.

    ``actor_user_id`` is ``None`` only for actions with no human actor (none
    exist in this slice yet; every instrumented action today has a verified
    caller). ``data`` is validated by :func:`_check_data_safety` before the
    INSERT: a violation raises before anything is written, rolling back the
    whole caller transaction (the audited mutation included) rather than
    silently dropping the secret or writing a partial trail.
    """
    payload = data or {}
    _check_data_safety(payload)
    await session.execute(
        text(
            "INSERT INTO audit_log "
            "(id, org_id, actor_user_id, action, target_type, target_id, data) "
            "VALUES (:id, :oid, :actor, :action, :ttype, :tid, CAST(:data AS jsonb))"
        ),
        {
            "id": str(uuid.uuid4()),
            "oid": str(org_id),
            "actor": str(actor_user_id) if actor_user_id is not None else None,
            "action": action,
            "ttype": target_type,
            "tid": target_id,
            "data": json.dumps(payload),
        },
    )


def _entry_dict(row: object) -> dict:
    return {
        "id": row.id,  # type: ignore[attr-defined]
        "actor_user_id": row.actor_user_id,  # type: ignore[attr-defined]
        "action": row.action,  # type: ignore[attr-defined]
        "target_type": row.target_type,  # type: ignore[attr-defined]
        "target_id": row.target_id,  # type: ignore[attr-defined]
        "data": row.data,  # type: ignore[attr-defined]
        "created_at": row.created_at,  # type: ignore[attr-defined]
    }


async def list_audit_log(
    org_id: uuid.UUID,
    *,
    action: str | None = None,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
    session_factory=None,
) -> dict:
    """Return a page of ``org_id``'s audit log, newest first, RLS-scoped.

    ``action``, if given, is an EXACT match against the stored action string
    (no wildcard/partial matching -- the UI's filter, if any, offers the closed
    :data:`ACTIONS` set, so a typo simply yields zero rows rather than a 400:
    this is a read filter, not a validated write).

    Actor emails are NOT attached here (that is the route layer's job, via
    ``accounts.load_users_by_ids`` on the RLS-exempt ``users`` table, mirroring
    every other cross-table email attachment in this codebase) so this function
    stays a single RLS-scoped read.
    """
    from app.db import tenant_session  # local import: avoids a module cycle risk

    where = ["1 = 1"]
    params: dict[str, Any] = {}
    if action is not None:
        where.append("action = :action")
        params["action"] = action
    where_sql = " AND ".join(where)
    offset = (page - 1) * per_page

    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        total = (
            await session.execute(
                text(f"SELECT count(*) FROM audit_log WHERE {where_sql}"), params  # noqa: S608 - where_sql is built from fixed literals, not user input
            )
        ).scalar_one()
        rows = (
            await session.execute(
                text(
                    "SELECT id, actor_user_id, action, target_type, target_id, "
                    "data, created_at FROM audit_log "
                    f"WHERE {where_sql} "  # noqa: S608 - see above
                    "ORDER BY created_at DESC, id DESC "
                    "LIMIT :limit OFFSET :offset"
                ),
                {**params, "limit": per_page, "offset": offset},
            )
        ).all()
    return {
        "entries": [_entry_dict(row) for row in rows],
        "total": int(total),
        "page": page,
        "per_page": per_page,
    }
