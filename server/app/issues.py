"""Issue services: list, detail, status actions, delete, assignment, comments.

Route handlers in ``app/routes/issues.py`` stay thin and call into these
functions. Like ``app/projects.py`` every function accepts an optional
``session_factory`` so integration tests can bind a NON-superuser engine and
exercise the real Row Level Security policies (production callers omit it).

SESSION CHOICE PER OPERATION (grounded in app/db.py's docstring): every function
here is org-scoped and runs through ``tenant_session(org_id)``. The ``org_id`` is
always the VERIFIED id from an ``OrgContext`` (require_org_member /
require_org_admin proved the caller's membership), never client input. RLS then
filters every statement by that org, so no handler writes ``WHERE org_id = ...``
by hand: a project, issue, or event belonging to another org is simply invisible
and surfaces as a 404, not a cross-tenant read.

PROJECT SCOPE: every read/action first confirms the project row is visible in the
current tenant session (same pattern as ``projects.get_project``); a missing or
other-org project returns None -> 404. Issue and event queries then additionally
match on ``project_id`` so an issue id from a different project in the SAME org
cannot be reached through the wrong project's URL.

OCCURRENCE COHERENCE (hard project gate): the detail response carries exactly ONE
occurrence data structure -- a zero-filled list of ``{day, count}`` for the last
``OCCURRENCE_WINDOW_DAYS`` days, computed server-side from a single grouped query.
Every occurrence display in the UI (the sparkline AND the "N events" total) MUST
derive from THIS one array; there is no second query and no separately-computed
total.

DELETE SEMANTICS (admin): ``delete_issue`` removes ONLY the issue row. The
``events`` table is a raw, daily-partitioned hot table with NO foreign key to
``issues`` (see migration 0001), so deleting an issue does NOT cascade to its
event rows and MUST NOT: an inline mass DELETE across partitions would be
expensive and is unnecessary. Orphaned event rows keep their ``issue_id`` value
and expire naturally via the partition-retention job. ``issue_comments`` (FK with
ON DELETE CASCADE) are removed by the database as a side effect.

SECRETS / PII HYGIENE: this module logs nothing. It returns stored event payloads
to the authorized caller but never logs them. Comment bodies and member emails
are likewise never logged; only ids ever reach a log line.

ASSIGNMENT (member action): ``assign_issue`` sets ``issues.assigned_to`` to a
user id or None (unassign). The candidate user id MUST be a member of the SAME
org -- verified via ``org_memberships`` inside the same ``tenant_session`` the
UPDATE runs in, never trusted from the client -- so an issue can never be
assigned to an outsider. The route answers 400 (not 404/403) for a non-member
candidate, since the ISSUE itself was found; it is the assignee that is invalid.

COMMENTS (member action): ``issue_comments`` has an FK CASCADE to ``issues``
(migration 0001), so deleting an issue removes its comments; no extra cleanup is
needed here. ``add_comment`` stamps ``author`` from the verified session user
(never client input). Newest-last ordering (``created_at ASC, id ASC``) matches a
chat-style thread read top-to-bottom.
"""

import datetime
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import accounts, audit
from app.db import tenant_session

# --- FLAGGED DEFAULTS (governor review) ---------------------------------------
# The comment body length cap. 5000 chars comfortably fits a stack trace snippet
# or a paragraph of triage notes without becoming an unbounded blob; chosen per
# the brief's "1..5000 chars" instruction rather than measured against real
# usage (no prior Crashlens comment data exists yet).
COMMENT_BODY_MIN_LENGTH = 1
COMMENT_BODY_MAX_LENGTH = 5000

# --- FLAGGED DEFAULTS (governor review) ---------------------------------------
# The status a filter tab may request. "all" means "no status predicate".
_ISSUE_STATUSES = ("unresolved", "resolved", "ignored", "regressed")
STATUS_FILTERS = (*_ISSUE_STATUSES, "all")
DEFAULT_STATUS_FILTER = "unresolved"

# Sort keys accepted from the client, mapped to a whitelisted (injection-safe)
# ORDER BY fragment. A stable ``id`` tiebreaker keeps pagination deterministic
# when many issues share the same last_seen / first_seen / count.
_SORT_ORDER_BY = {
    "last_seen": "last_seen DESC, id DESC",
    "first_seen": "first_seen DESC, id DESC",
    "count": "event_count DESC, id DESC",
}
DEFAULT_SORT = "last_seen"

DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 100

# The occurrence chart window and the recent-events tail length.
OCCURRENCE_WINDOW_DAYS = 14
RECENT_EVENTS_LIMIT = 25


# ==============================================================================
# Pure helpers (no I/O; unit-tested without a database).
# ==============================================================================


def normalize_status_filter(value: str | None) -> str:
    """Return a valid status filter, defaulting a missing value.

    Raises ``ValueError`` for an unrecognized status so the route can answer 400
    rather than silently ignoring a typo'd filter.
    """
    if value is None or value == "":
        return DEFAULT_STATUS_FILTER
    if value not in STATUS_FILTERS:
        raise ValueError(f"Unknown status filter: {value!r}")
    return value


def normalize_sort(value: str | None) -> str:
    """Return a valid sort key, defaulting a missing value; raise on unknown."""
    if value is None or value == "":
        return DEFAULT_SORT
    if value not in _SORT_ORDER_BY:
        raise ValueError(f"Unknown sort: {value!r}")
    return value


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


def zero_fill_occurrences(
    counts_by_day: dict[datetime.date, int],
    today: datetime.date,
    days: int = OCCURRENCE_WINDOW_DAYS,
) -> list[dict]:
    """Return a dense, oldest-first list of ``{day, count}`` for the window.

    The window is the ``days`` calendar days ending on ``today`` (inclusive).
    Days with no events are filled with a zero count, so the resulting array
    always has exactly ``days`` entries. This is the SINGLE occurrence structure
    the UI renders both its sparkline and its total from.
    """
    start = today - datetime.timedelta(days=days - 1)
    return [
        {
            "day": (start + datetime.timedelta(days=offset)).isoformat(),
            "count": int(counts_by_day.get(start + datetime.timedelta(days=offset), 0)),
        }
        for offset in range(days)
    ]


def validate_comment_body(body: str) -> str:
    """Return the trimmed comment body, or raise ``ValueError`` if invalid.

    Enforces the ``[COMMENT_BODY_MIN_LENGTH, COMMENT_BODY_MAX_LENGTH]`` char
    bound on the TRIMMED body, so whitespace-only input is rejected as empty.
    """
    trimmed = body.strip()
    if len(trimmed) < COMMENT_BODY_MIN_LENGTH or len(trimmed) > COMMENT_BODY_MAX_LENGTH:
        raise ValueError(
            "Comment must be between "
            f"{COMMENT_BODY_MIN_LENGTH} and {COMMENT_BODY_MAX_LENGTH} characters."
        )
    return trimmed


def _coerce_payload(value: Any) -> Any:
    """Return a stored jsonb payload as a Python object.

    The asyncpg driver may hand a jsonb column back as a decoded object or as a
    raw JSON string depending on codec setup; normalize to the decoded object so
    the API always returns structured JSON. A non-string, non-decodable value is
    returned unchanged (defensive).
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


# ==============================================================================
# Data-layer functions.
# ==============================================================================


async def _load_project_row(session: AsyncSession, project_id: uuid.UUID) -> object | None:
    """Return the project row visible in the current tenant session, or None.

    RLS on the open ``tenant_session`` scopes visibility to the org, so a project
    in another org is simply not found (a 404, not a cross-tenant read).
    """
    return (
        await session.execute(
            text("SELECT id FROM projects WHERE id = :pid"),
            {"pid": str(project_id)},
        )
    ).one_or_none()


def _issue_dict(row: object) -> dict:
    """Shape one issue row into the list/detail base fields.

    ``resolved_in_release`` / ``regressed_in_release`` are the release-tracking
    fields (W5-02): the release an Issue was marked fixed in, and the release it
    came back in on a regression. Both are NULL until set. They come from the one
    shared shape so the list and detail views stay coherent.
    """
    assigned_to = row.assigned_to  # type: ignore[attr-defined]
    return {
        "id": str(row.id),  # type: ignore[attr-defined]
        "title": row.title,  # type: ignore[attr-defined]
        "level": row.level,  # type: ignore[attr-defined]
        "status": row.status,  # type: ignore[attr-defined]
        "first_seen": row.first_seen.isoformat(),  # type: ignore[attr-defined]
        "last_seen": row.last_seen.isoformat(),  # type: ignore[attr-defined]
        "event_count": int(row.event_count),  # type: ignore[attr-defined]
        "assigned_to": str(assigned_to) if assigned_to is not None else None,
        "resolved_in_release": row.resolved_in_release,  # type: ignore[attr-defined]
        "regressed_in_release": row.regressed_in_release,  # type: ignore[attr-defined]
    }


async def list_issues(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    status_filter: str = DEFAULT_STATUS_FILTER,
    q: str | None = None,
    sort: str = DEFAULT_SORT,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict | None:
    """Return a page of the project's issues, or None if the project is absent.

    ``status_filter`` / ``sort`` MUST already be validated (see
    ``normalize_status_filter`` / ``normalize_sort``); ``page`` / ``per_page`` are
    clamped by the caller. Filtering is by status (unless ``all``) and an optional
    case-insensitive substring ``q`` on the title. Returns
    ``{issues, total, page, per_page}`` with ``total`` the count under the same
    filters (for pagination), all RLS-scoped to ``org_id``.
    """
    order_by = _SORT_ORDER_BY[sort]
    where = ["project_id = :pid"]
    params: dict[str, Any] = {"pid": str(project_id)}
    if status_filter != "all":
        where.append("status = :status")
        params["status"] = status_filter
    if q:
        where.append("title ILIKE :q")
        params["q"] = f"%{q}%"
    where_sql = " AND ".join(where)
    offset = (page - 1) * per_page

    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        if await _load_project_row(session, project_id) is None:
            return None
        total = (
            await session.execute(
                text(f"SELECT count(*) FROM issues WHERE {where_sql}"), params
            )
        ).scalar_one()
        rows = (
            await session.execute(
                text(
                    "SELECT id, title, level, status, first_seen, last_seen, "
                    "event_count, assigned_to, resolved_in_release, "
                    "regressed_in_release FROM issues "
                    f"WHERE {where_sql} ORDER BY {order_by} "
                    "LIMIT :limit OFFSET :offset"
                ),
                {**params, "limit": per_page, "offset": offset},
            )
        ).all()
    return {
        "issues": [_issue_dict(row) for row in rows],
        "total": int(total),
        "page": page,
        "per_page": per_page,
    }


async def get_issue(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    issue_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict | None:
    """Return the full issue detail, or None if the project or issue is absent.

    Beyond the issue's own fields the detail carries: ``latest_event`` (the newest
    stored event's full payload plus its received_at/environment/release),
    ``recent_events`` (the last ``RECENT_EVENTS_LIMIT`` events without payloads),
    and ``occurrences`` (the single zero-filled ``OCCURRENCE_WINDOW_DAYS``-day
    ``{day, count}`` array the UI renders both its chart and its total from).
    """
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        if await _load_project_row(session, project_id) is None:
            return None
        issue_row = (
            await session.execute(
                text(
                    "SELECT id, title, level, status, first_seen, last_seen, "
                    "event_count, assigned_to, resolved_in_release, "
                    "regressed_in_release FROM issues "
                    "WHERE id = :iid AND project_id = :pid"
                ),
                {"iid": str(issue_id), "pid": str(project_id)},
            )
        ).one_or_none()
        if issue_row is None:
            return None

        latest_row = (
            await session.execute(
                text(
                    "SELECT event_id, received_at, environment, release, level, payload "
                    "FROM events WHERE project_id = :pid AND issue_id = :iid "
                    "ORDER BY received_at DESC LIMIT 1"
                ),
                {"pid": str(project_id), "iid": str(issue_id)},
            )
        ).one_or_none()

        recent_rows = (
            await session.execute(
                text(
                    "SELECT event_id, received_at, environment, release, level "
                    "FROM events WHERE project_id = :pid AND issue_id = :iid "
                    "ORDER BY received_at DESC LIMIT :limit"
                ),
                {
                    "pid": str(project_id),
                    "iid": str(issue_id),
                    "limit": RECENT_EVENTS_LIMIT,
                },
            )
        ).all()

        today = datetime.datetime.now(datetime.UTC).date()
        window_start = datetime.datetime.combine(
            today - datetime.timedelta(days=OCCURRENCE_WINDOW_DAYS - 1),
            datetime.time.min,
            tzinfo=datetime.UTC,
        )
        occ_rows = (
            await session.execute(
                text(
                    "SELECT (received_at AT TIME ZONE 'UTC')::date AS day, count(*) AS c "
                    "FROM events WHERE project_id = :pid AND issue_id = :iid "
                    "AND received_at >= :start "
                    "GROUP BY (received_at AT TIME ZONE 'UTC')::date"
                ),
                {"pid": str(project_id), "iid": str(issue_id), "start": window_start},
            )
        ).all()

    detail = _issue_dict(issue_row)
    detail["latest_event"] = (
        {
            "event_id": str(latest_row.event_id),
            "received_at": latest_row.received_at.isoformat(),
            "environment": latest_row.environment,
            "release": latest_row.release,
            "level": latest_row.level,
            "payload": _coerce_payload(latest_row.payload),
        }
        if latest_row is not None
        else None
    )
    detail["recent_events"] = [
        {
            "event_id": str(r.event_id),
            "received_at": r.received_at.isoformat(),
            "environment": r.environment,
            "release": r.release,
            "level": r.level,
        }
        for r in recent_rows
    ]
    counts_by_day = {r.day: int(r.c) for r in occ_rows}
    detail["occurrences"] = zero_fill_occurrences(counts_by_day, today)
    detail["assigned_to_email"] = await _load_assignee_email(
        detail["assigned_to"], session_factory=session_factory
    )
    return detail


async def _load_assignee_email(
    assigned_to: str | None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> str | None:
    """Return the assignee's email, or None if unassigned. Same discipline as
    ``projects.list_members``: emails live on the RLS-exempt ``users`` table and
    are attached via ``accounts.load_users_by_ids`` after the org-scoped read.
    """
    if assigned_to is None:
        return None
    assignee_id = uuid.UUID(assigned_to)
    emails = await accounts.load_users_by_ids(
        [assignee_id], session_factory=session_factory
    )
    return emails.get(assignee_id)


# The three action verbs map to the SET clause that applies them, including the
# release-tracking bookkeeping (W5-02):
#   * resolve captures the fix release = the project's LATEST release (MAX
#     created_at, i.e. the most recently first-seen version), or NULL when the
#     project has no releases yet. It ALSO clears regressed_in_release in the
#     SAME UPDATE: an issue that is currently resolved has, by definition, not
#     come back, so the "came back in" marker must never linger next to a fresh
#     "fixed in" (coherent-views governor ruling on W5-02).
#   * ignore clears regressed_in_release (the "came back in" badge should not
#     linger on a muted Issue) but leaves any recorded fix release.
#   * reopen restores unresolved and clears BOTH release fields (a fresh start).
# Reopen restores from ANY state; all are idempotent (re-applying the same status
# rewrites the same values). Each fragment is a fixed, server-controlled string
# (never client input), so interpolating it carries no injection risk.
_ACTION_SET_SQL = {
    "resolve": (
        "status = 'resolved', regressed_in_release = NULL, "
        "resolved_in_release = ("
        "SELECT version FROM releases WHERE project_id = :pid "
        "ORDER BY created_at DESC, version DESC LIMIT 1)"
    ),
    "ignore": "status = 'ignored', regressed_in_release = NULL",
    "reopen": (
        "status = 'unresolved', resolved_in_release = NULL, "
        "regressed_in_release = NULL"
    ),
}

_STATUS_RETURNING = (
    "RETURNING id, title, level, status, first_seen, last_seen, "
    "event_count, assigned_to, resolved_in_release, regressed_in_release"
)

# Maps the action verb this function accepts to the audit action name it
# records (dot notation, per app/audit.py's ACTIONS).
_STATUS_AUDIT_ACTIONS = {
    "resolve": "issue.resolved",
    "ignore": "issue.ignored",
    "reopen": "issue.reopened",
}


async def set_issue_status(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    issue_id: uuid.UUID,
    action: str,
    *,
    actor_user_id: uuid.UUID | None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict | None:
    """Apply a status ``action`` (resolve|ignore|reopen) and return the issue.

    Returns the updated issue dict, or None if the project or issue is not
    visible in this org (a 404, not a cross-tenant write). Idempotent: applying
    the status an issue already holds simply rewrites the same values. On
    ``resolve`` the project's latest release is recorded as the fix release and
    the came-back-in release is cleared (a resolved issue has not come back); on
    ``reopen``/``ignore`` the came-back-in release is cleared (reopen also clears
    the fix release). RLS scopes the UPDATE to ``org_id`` and it is additionally
    matched on ``project_id`` so an issue reached through the wrong project URL is
    not touched. The ``releases`` subquery on ``resolve`` runs in the same
    RLS-scoped session, so only this org's releases are visible to it.

    ``actor_user_id`` is recorded on the corresponding "issue.resolved" /
    "issue.ignored" / "issue.reopened" audit row (via
    :data:`_STATUS_AUDIT_ACTIONS`), written only when the issue was actually
    found and updated.
    """
    set_clause = _ACTION_SET_SQL[action]
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        if await _load_project_row(session, project_id) is None:
            return None
        row = (
            await session.execute(
                text(
                    f"UPDATE issues SET {set_clause} "
                    "WHERE id = :iid AND project_id = :pid "
                    f"{_STATUS_RETURNING}"
                ),
                {"iid": str(issue_id), "pid": str(project_id)},
            )
        ).one_or_none()
        if row is None:
            return None
        await audit.record(
            session,
            org_id=org_id,
            actor_user_id=actor_user_id,
            action=_STATUS_AUDIT_ACTIONS[action],
            target_type="issue",
            target_id=str(issue_id),
            data={"project_id": str(project_id)},
        )
    return _issue_dict(row)


# --- Assignment (member) -------------------------------------------------------
class InvalidAssigneeError(ValueError):
    """Raised when ``user_id`` passed to ``assign_issue`` is not an org member.

    The route maps this to 400 (the ISSUE was found; the candidate assignee was
    not), distinct from the 404 a missing project/issue produces.
    """


async def assign_issue(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    issue_id: uuid.UUID,
    user_id: uuid.UUID | None,
    *,
    actor_user_id: uuid.UUID | None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict | None:
    """Assign (or unassign, ``user_id=None``) an issue and return its detail.

    Returns None if the project or issue is not visible in this org (a 404).
    Raises ``InvalidAssigneeError`` if ``user_id`` is not a member of ``org_id``
    -- membership is verified against ``org_memberships`` INSIDE the same
    ``tenant_session`` the UPDATE runs in, so an issue can never be assigned to
    an outsider even under a race.

    ``actor_user_id`` is the caller performing the assignment, recorded on the
    "issue.assigned" audit row (distinct from ``user_id``, the ASSIGNEE);
    written only when the issue was actually found and updated.
    """
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        if await _load_project_row(session, project_id) is None:
            return None
        if user_id is not None:
            is_member = (
                await session.execute(
                    text(
                        "SELECT 1 FROM org_memberships WHERE user_id = :uid"
                    ),
                    {"uid": str(user_id)},
                )
            ).one_or_none()
            if is_member is None:
                raise InvalidAssigneeError(
                    "The assignee must be a member of this organization."
                )
        row = (
            await session.execute(
                text(
                    "UPDATE issues SET assigned_to = :assignee "
                    "WHERE id = :iid AND project_id = :pid "
                    "RETURNING id"
                ),
                {
                    "assignee": str(user_id) if user_id is not None else None,
                    "iid": str(issue_id),
                    "pid": str(project_id),
                },
            )
        ).one_or_none()
        if row is None:
            return None
        await audit.record(
            session,
            org_id=org_id,
            actor_user_id=actor_user_id,
            action="issue.assigned",
            target_type="issue",
            target_id=str(issue_id),
            data={"assigned_to": str(user_id) if user_id is not None else None},
        )
    return await get_issue(
        org_id, project_id, issue_id, session_factory=session_factory
    )


# --- Comments (member) ----------------------------------------------------------
def _comment_dict(row: object, email: str | None) -> dict:
    # ``author`` is NULL when the authoring user was deleted (FK ON DELETE SET
    # NULL, coherent since revision 0004 dropped the NOT NULL): both author
    # fields surface as None and the UI renders a plain-language placeholder.
    author = row.author  # type: ignore[attr-defined]
    return {
        "id": str(row.id),  # type: ignore[attr-defined]
        "author_id": str(author) if author is not None else None,
        "author_email": email,
        "body": row.body,  # type: ignore[attr-defined]
        "created_at": row.created_at.isoformat(),  # type: ignore[attr-defined]
    }


async def list_comments(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    issue_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> list[dict] | None:
    """Return the issue's comments oldest-first (chronological), or None if the
    project or issue is not visible in this org (a 404).
    """
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        if await _load_project_row(session, project_id) is None:
            return None
        issue_row = (
            await session.execute(
                text("SELECT id FROM issues WHERE id = :iid AND project_id = :pid"),
                {"iid": str(issue_id), "pid": str(project_id)},
            )
        ).one_or_none()
        if issue_row is None:
            return None
        rows = (
            await session.execute(
                text(
                    "SELECT id, author, body, created_at FROM issue_comments "
                    "WHERE issue_id = :iid ORDER BY created_at ASC, id ASC"
                ),
                {"iid": str(issue_id)},
            )
        ).all()
    # NULL authors (deleted users) are skipped in the email lookup; their
    # ``emails.get(None)`` below naturally resolves to None.
    author_ids = [row.author for row in rows if row.author is not None]
    emails = await accounts.load_users_by_ids(
        author_ids, session_factory=session_factory
    )
    return [_comment_dict(row, emails.get(row.author)) for row in rows]


async def add_comment(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    issue_id: uuid.UUID,
    author_id: uuid.UUID,
    body: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict | None:
    """Create a comment authored by ``author_id`` (the verified session user).

    ``body`` MUST already be validated by ``validate_comment_body``. Returns the
    created comment, or None if the project or issue is not visible in this org
    (a 404).
    """
    comment_id = uuid.uuid4()
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        if await _load_project_row(session, project_id) is None:
            return None
        issue_row = (
            await session.execute(
                text("SELECT id FROM issues WHERE id = :iid AND project_id = :pid"),
                {"iid": str(issue_id), "pid": str(project_id)},
            )
        ).one_or_none()
        if issue_row is None:
            return None
        row = (
            await session.execute(
                text(
                    "INSERT INTO issue_comments (id, org_id, issue_id, author, body) "
                    "VALUES (:id, :oid, :iid, :author, :body) "
                    "RETURNING id, author, body, created_at"
                ),
                {
                    "id": str(comment_id),
                    "oid": str(org_id),
                    "iid": str(issue_id),
                    "author": str(author_id),
                    "body": body,
                },
            )
        ).one()
    emails = await accounts.load_users_by_ids(
        [author_id], session_factory=session_factory
    )
    return _comment_dict(row, emails.get(author_id))


async def delete_issue(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    issue_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> bool:
    """Delete ONLY the issue row. Return True if one was deleted.

    Deliberately does NOT cascade-delete this issue's ``events`` rows: ``events``
    has no foreign key to ``issues`` (raw partitioned hot table), so no cascade
    fires, and an inline cross-partition mass DELETE would be expensive. Orphaned
    event rows keep their ``issue_id`` and expire via the partition-retention job.
    RLS scopes the DELETE to ``org_id`` and it is matched on ``project_id``, so an
    issue in another org (or reached through the wrong project) affects zero rows
    (reported as a 404, not a cross-tenant delete).

    ``actor_user_id`` is recorded on the "issue.deleted" audit row, written only
    when an issue was actually found and deleted; its ``title`` is captured via
    ``RETURNING`` for the audit trail (the underlying events are unaffected, per
    the module docstring, so this fact remains meaningful after the delete).
    """
    async with tenant_session(str(org_id), session_factory=session_factory) as session:
        row = (
            await session.execute(
                text(
                    "DELETE FROM issues WHERE id = :iid AND project_id = :pid "
                    "RETURNING title"
                ),
                {"iid": str(issue_id), "pid": str(project_id)},
            )
        ).one_or_none()
        if row is None:
            return False
        await audit.record(
            session,
            org_id=org_id,
            actor_user_id=actor_user_id,
            action="issue.deleted",
            target_type="issue",
            target_id=str(issue_id),
            data={"project_id": str(project_id), "title": row.title},
        )
    return True
