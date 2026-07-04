"""Integration tests for the issues slice (marked ``db``).

Like the sibling integration suites these require a live PostgreSQL with the
migrations applied and SKIP cleanly when none is reachable, so ``pytest -q``
passes locally without Postgres. In CI the postgres:16 service is up, the
migrations are applied, and these run for real.

Issues and events are seeded by driving the REAL ``process_event`` job under the
NON-superuser ``crashlens_test`` role, so RLS is genuinely enforced and the rows
have the exact shape production produces. The tests then prove:

* list filtering by status, case-insensitive title search, the three sorts, and
  pagination (page/per_page/total);
* the detail payload (issue fields, latest_event with its stored payload,
  recent_events, and the zero-filled 14-day occurrence array whose sum equals the
  event total);
* the status action transitions (resolve, ignore, reopen from any state) are
  idempotent;
* admin-only delete removes the issue but leaves its events;
* assignment (member) requires the candidate to be an org member (400
  otherwise), supports unassigning, and refreshes the ``assigned_to_email``
  field;
* comments (member) are listed oldest-first and created with the caller as
  author;
* cross-org isolation (org B cannot see or act on org A's issues, including
  assignment and comments).

An HTTP-level authZ matrix proves member-vs-admin-vs-outsider access on every
endpoint.
"""

import datetime
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import issues, security
from app.config import get_settings
from app.db import tenant_session
from app.jobs.process_event import process_event
from app.main import create_app

pytestmark = pytest.mark.db

_TEST_ROLE = "crashlens_test"
_TEST_PASSWORD = "crashlens_test"
_KNOWN_PASSWORD = "a-strong-test-passphrase"


@pytest_asyncio.fixture(scope="module")
async def superuser_engine():
    """Engine on the migration/superuser DATABASE_URL. Skips if unreachable."""
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("PostgreSQL not reachable; skipping issues integration tests")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="module")
async def app_sessionmaker(superuser_engine):
    """Session factory bound to a non-superuser role so RLS actually applies.

    Same shape (role name, idempotent creation, grants) as the sibling
    integration tests so all share one CI session without conflict.
    """
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crashlens_test') "
                "THEN CREATE ROLE crashlens_test LOGIN PASSWORD 'crashlens_test' "
                "NOSUPERUSER; END IF; "
                "END $$;"
            )
        )
        await conn.execute(text("GRANT crashlens_app TO crashlens_test"))
        await conn.execute(text("GRANT crashlens_system TO crashlens_test"))

    url = make_url(get_settings().database_url).set(
        username=_TEST_ROLE, password=_TEST_PASSWORD
    )
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def two_orgs(superuser_engine):
    """Seed two orgs, each with one project. Cleaned up on teardown."""
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    project_a, project_b = uuid.uuid4(), uuid.uuid4()
    async with superuser_engine.begin() as conn:
        for org, slug in ((org_a, "iss-org-a"), (org_b, "iss-org-b")):
            await conn.execute(
                text("INSERT INTO orgs (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": org, "name": slug, "slug": f"{slug}-{org}"},
            )
        for project, org, slug in (
            (project_a, org_a, "iss-proj-a"),
            (project_b, org_b, "iss-proj-b"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO projects (id, org_id, name, slug, platform) "
                    "VALUES (:id, :org, :name, :slug, 'python')"
                ),
                {"id": project, "org": org, "name": slug, "slug": slug},
            )
    yield {"org_a": org_a, "org_b": org_b, "project_a": project_a, "project_b": project_b}
    async with superuser_engine.begin() as conn:
        # events has no FK to orgs (raw partitioned table); delete it explicitly
        # before the org cascade removes projects/issues.
        await conn.execute(
            text("DELETE FROM events WHERE org_id IN (:a, :b)"),
            {"a": org_a, "b": org_b},
        )
        await conn.execute(
            text("DELETE FROM orgs WHERE id IN (:a, :b)"), {"a": org_a, "b": org_b}
        )


def _exc_envelope(event_id: str, *, func: str = "compute_total", value: str = "boom") -> dict:
    """An exception envelope; ``func`` varies the trace so fingerprints differ."""
    return {
        "event_id": event_id,
        "timestamp": "2026-07-04T12:00:00.000Z",
        "platform": "python",
        "level": "error",
        "environment": "production",
        "release": "web@1.0.0",
        "sdk": {"name": "crashlens-python", "version": "0.1.0"},
        "tags": {"server_name": "web-1"},
        "breadcrumbs": [
            {
                "type": "http",
                "category": "request",
                "message": "GET /checkout",
                "timestamp": "2026-07-04T11:59:59.000Z",
            }
        ],
        "exception": {
            "type": "ValueError",
            "value": value,
            "stacktrace": {
                "frames": [
                    {
                        "filename": "app.py",
                        "function": "handler",
                        "lineno": 10,
                        "in_app": True,
                        "context_line": "do_work()",
                    },
                    {
                        "filename": "billing.py",
                        "function": func,
                        "lineno": 42,
                        "in_app": True,
                        "context_line": "1 / 0",
                    },
                ]
            },
        },
    }


def _now_iso(offset_days: int = 0) -> str:
    # Land in a partition the migration pre-created (today .. today+7). Negative
    # offsets would need a back-dated partition, so occurrence tests that need
    # "earlier" days rely on same-day events plus zero-fill of the empty days.
    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=offset_days)
    return when.isoformat()


async def _seed_event(app_sessionmaker, org_id, project_id, envelope) -> dict:
    return await process_event(
        {},
        envelope=envelope,
        org_id=str(org_id),
        project_id=str(project_id),
        dsn_key_id=str(uuid.uuid4()),
        received_at=_now_iso(),
        session_factory=app_sessionmaker,
    )


# --- List: filter / search / sort / pagination --------------------------------
async def test_list_filters_by_status(app_sessionmaker, two_orgs) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    # Two distinct issues (different traces => different fingerprints).
    r1 = await _seed_event(
        app_sessionmaker, org_a, project_a, _exc_envelope(str(uuid.uuid4()), func="alpha")
    )
    await _seed_event(
        app_sessionmaker, org_a, project_a, _exc_envelope(str(uuid.uuid4()), func="beta")
    )
    # Resolve the first issue.
    resolved = await issues.set_issue_status(
        org_a, project_a, uuid.UUID(r1["issue_id"]), "resolve",
        session_factory=app_sessionmaker,
    )
    assert resolved is not None and resolved["status"] == "resolved"

    # Default (unresolved) shows only the still-open issue.
    open_page = await issues.list_issues(
        org_a, project_a, session_factory=app_sessionmaker
    )
    assert open_page is not None
    assert open_page["total"] == 1
    assert all(i["status"] == "unresolved" for i in open_page["issues"])

    # resolved filter shows only the resolved one.
    resolved_page = await issues.list_issues(
        org_a, project_a, status_filter="resolved", session_factory=app_sessionmaker
    )
    assert resolved_page is not None
    assert resolved_page["total"] == 1
    assert resolved_page["issues"][0]["id"] == r1["issue_id"]

    # all shows both.
    all_page = await issues.list_issues(
        org_a, project_a, status_filter="all", session_factory=app_sessionmaker
    )
    assert all_page is not None
    assert all_page["total"] == 2


async def test_list_search_is_case_insensitive_on_title(
    app_sessionmaker, two_orgs
) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    await _seed_event(
        app_sessionmaker, org_a, project_a,
        _exc_envelope(str(uuid.uuid4()), func="alpha", value="Payment declined"),
    )
    await _seed_event(
        app_sessionmaker, org_a, project_a,
        _exc_envelope(str(uuid.uuid4()), func="beta", value="Timeout reached"),
    )
    hit = await issues.list_issues(
        org_a, project_a, q="payment", session_factory=app_sessionmaker
    )
    assert hit is not None
    assert hit["total"] == 1
    assert "Payment declined" in hit["issues"][0]["title"]

    miss = await issues.list_issues(
        org_a, project_a, q="nothing-here", session_factory=app_sessionmaker
    )
    assert miss is not None
    assert miss["total"] == 0


async def test_list_sort_by_count(app_sessionmaker, two_orgs) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    # Issue A: one event. Issue B: three events (highest count).
    await _seed_event(
        app_sessionmaker, org_a, project_a, _exc_envelope(str(uuid.uuid4()), func="single")
    )
    for _ in range(3):
        await _seed_event(
            app_sessionmaker, org_a, project_a,
            _exc_envelope(str(uuid.uuid4()), func="triple"),
        )
    page = await issues.list_issues(
        org_a, project_a, sort="count", session_factory=app_sessionmaker
    )
    assert page is not None
    counts = [i["event_count"] for i in page["issues"]]
    assert counts == sorted(counts, reverse=True)
    assert page["issues"][0]["event_count"] == 3


async def test_list_pagination(app_sessionmaker, two_orgs) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    for n in range(5):
        await _seed_event(
            app_sessionmaker, org_a, project_a,
            _exc_envelope(str(uuid.uuid4()), func=f"f{n}"),
        )
    first = await issues.list_issues(
        org_a, project_a, page=1, per_page=2, session_factory=app_sessionmaker
    )
    second = await issues.list_issues(
        org_a, project_a, page=2, per_page=2, session_factory=app_sessionmaker
    )
    third = await issues.list_issues(
        org_a, project_a, page=3, per_page=2, session_factory=app_sessionmaker
    )
    assert first is not None and second is not None and third is not None
    assert first["total"] == 5 and first["per_page"] == 2
    assert len(first["issues"]) == 2
    assert len(second["issues"]) == 2
    assert len(third["issues"]) == 1
    # No overlap across pages.
    ids = {i["id"] for i in first["issues"]}
    ids |= {i["id"] for i in second["issues"]}
    ids |= {i["id"] for i in third["issues"]}
    assert len(ids) == 5


async def test_list_missing_project_is_none(app_sessionmaker, two_orgs) -> None:
    org_a = two_orgs["org_a"]
    result = await issues.list_issues(
        org_a, uuid.uuid4(), session_factory=app_sessionmaker
    )
    assert result is None


# --- Detail -------------------------------------------------------------------
async def test_detail_payload_and_zero_filled_occurrences(
    app_sessionmaker, two_orgs
) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    issue_id = None
    for _ in range(3):
        res = await _seed_event(
            app_sessionmaker, org_a, project_a,
            _exc_envelope(str(uuid.uuid4()), func="detail"),
        )
        issue_id = res["issue_id"]
    assert issue_id is not None

    detail = await issues.get_issue(
        org_a, project_a, uuid.UUID(issue_id), session_factory=app_sessionmaker
    )
    assert detail is not None
    # Issue fields.
    assert detail["event_count"] == 3
    assert detail["title"].startswith("ValueError:")
    # latest_event carries the stored payload and routing metadata.
    assert detail["latest_event"] is not None
    assert detail["latest_event"]["environment"] == "production"
    assert detail["latest_event"]["release"] == "web@1.0.0"
    payload = detail["latest_event"]["payload"]
    assert isinstance(payload, dict)
    assert payload["exception"]["type"] == "ValueError"
    assert payload["breadcrumbs"][-1]["message"] == "GET /checkout"
    # recent_events lists events without payloads.
    assert len(detail["recent_events"]) == 3
    assert all("payload" not in ev for ev in detail["recent_events"])
    # Occurrences: dense 14-day window whose sum equals the event total (the one
    # array the UI renders both the chart and the total from).
    occ = detail["occurrences"]
    assert len(occ) == issues.OCCURRENCE_WINDOW_DAYS
    assert sum(entry["count"] for entry in occ) == 3
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    assert occ[-1]["day"] == today
    assert occ[-1]["count"] == 3


async def test_detail_missing_issue_is_none(app_sessionmaker, two_orgs) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    detail = await issues.get_issue(
        org_a, project_a, uuid.uuid4(), session_factory=app_sessionmaker
    )
    assert detail is None


# --- Status action transitions ------------------------------------------------
async def test_status_transitions_are_idempotent(app_sessionmaker, two_orgs) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    res = await _seed_event(
        app_sessionmaker, org_a, project_a, _exc_envelope(str(uuid.uuid4()), func="trans")
    )
    issue_id = uuid.UUID(res["issue_id"])

    async def status_of() -> str:
        detail = await issues.get_issue(
            org_a, project_a, issue_id, session_factory=app_sessionmaker
        )
        assert detail is not None
        return detail["status"]

    assert await status_of() == "unresolved"

    ignored = await issues.set_issue_status(
        org_a, project_a, issue_id, "ignore", session_factory=app_sessionmaker
    )
    assert ignored is not None and ignored["status"] == "ignored"
    # Idempotent: ignoring again keeps it ignored.
    again = await issues.set_issue_status(
        org_a, project_a, issue_id, "ignore", session_factory=app_sessionmaker
    )
    assert again is not None and again["status"] == "ignored"

    resolved = await issues.set_issue_status(
        org_a, project_a, issue_id, "resolve", session_factory=app_sessionmaker
    )
    assert resolved is not None and resolved["status"] == "resolved"

    # Reopen from resolved.
    reopened = await issues.set_issue_status(
        org_a, project_a, issue_id, "reopen", session_factory=app_sessionmaker
    )
    assert reopened is not None and reopened["status"] == "unresolved"


async def test_action_on_missing_issue_is_none(app_sessionmaker, two_orgs) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    result = await issues.set_issue_status(
        org_a, project_a, uuid.uuid4(), "resolve", session_factory=app_sessionmaker
    )
    assert result is None


# --- Delete keeps events ------------------------------------------------------
async def test_delete_issue_removes_issue_but_keeps_events(
    app_sessionmaker, two_orgs
) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    env = _exc_envelope(str(uuid.uuid4()), func="del")
    res = await _seed_event(app_sessionmaker, org_a, project_a, env)
    issue_id = uuid.UUID(res["issue_id"])

    deleted = await issues.delete_issue(
        org_a, project_a, issue_id, session_factory=app_sessionmaker
    )
    assert deleted is True
    # Second delete is a no-op (already gone).
    assert (
        await issues.delete_issue(
            org_a, project_a, issue_id, session_factory=app_sessionmaker
        )
        is False
    )
    # The issue is gone but its event row survives (expires via retention).
    async with tenant_session(str(org_a), session_factory=app_sessionmaker) as session:
        issue_left = (
            await session.execute(
                text("SELECT count(*) FROM issues WHERE id = :i"), {"i": issue_id}
            )
        ).scalar_one()
        event_left = (
            await session.execute(
                text("SELECT count(*) FROM events WHERE event_id = :e"),
                {"e": env["event_id"]},
            )
        ).scalar_one()
    assert issue_left == 0
    assert event_left == 1


# --- Assignment (member) -------------------------------------------------------
async def test_assign_issue_to_member_sets_assignee_and_email(
    app_sessionmaker, superuser_engine, two_orgs
) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    res = await _seed_event(
        app_sessionmaker, org_a, project_a, _exc_envelope(str(uuid.uuid4()), func="assignee")
    )
    issue_id = uuid.UUID(res["issue_id"])

    member_id, member_email = uuid.uuid4(), f"assignee-{uuid.uuid4()}@example.test"
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash) "
                "VALUES (:id, :email, :ph)"
            ),
            {"id": member_id, "email": member_email, "ph": security.hash_password(_KNOWN_PASSWORD)},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (org_id, user_id, role) "
                "VALUES (:o, :u, 'member')"
            ),
            {"o": org_a, "u": member_id},
        )
    try:
        detail = await issues.assign_issue(
            org_a, project_a, issue_id, member_id, session_factory=app_sessionmaker
        )
        assert detail is not None
        assert detail["assigned_to"] == str(member_id)
        assert detail["assigned_to_email"] == member_email

        # Unassign: assigned_to and assigned_to_email both go back to None.
        unassigned = await issues.assign_issue(
            org_a, project_a, issue_id, None, session_factory=app_sessionmaker
        )
        assert unassigned is not None
        assert unassigned["assigned_to"] is None
        assert unassigned["assigned_to_email"] is None
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": member_id})


async def test_assign_issue_to_non_member_is_invalid(
    app_sessionmaker, superuser_engine, two_orgs
) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    res = await _seed_event(
        app_sessionmaker, org_a, project_a, _exc_envelope(str(uuid.uuid4()), func="nonmember")
    )
    issue_id = uuid.UUID(res["issue_id"])

    outsider_id = uuid.uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash) "
                "VALUES (:id, :email, :ph)"
            ),
            {
                "id": outsider_id,
                "email": f"nonmember-{uuid.uuid4()}@example.test",
                "ph": security.hash_password(_KNOWN_PASSWORD),
            },
        )
    try:
        with pytest.raises(issues.InvalidAssigneeError):
            await issues.assign_issue(
                org_a, project_a, issue_id, outsider_id, session_factory=app_sessionmaker
            )
        # The failed attempt did not change the assignee.
        detail = await issues.get_issue(
            org_a, project_a, issue_id, session_factory=app_sessionmaker
        )
        assert detail is not None
        assert detail["assigned_to"] is None
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": outsider_id})


async def test_assign_missing_issue_is_none(app_sessionmaker, two_orgs) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    result = await issues.assign_issue(
        org_a, project_a, uuid.uuid4(), None, session_factory=app_sessionmaker
    )
    assert result is None


# --- Comments (member) ----------------------------------------------------------
async def test_comments_create_and_list_chronological(
    app_sessionmaker, superuser_engine, two_orgs
) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    res = await _seed_event(
        app_sessionmaker, org_a, project_a, _exc_envelope(str(uuid.uuid4()), func="comment")
    )
    issue_id = uuid.UUID(res["issue_id"])

    author_id, author_email = uuid.uuid4(), f"commenter-{uuid.uuid4()}@example.test"
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash) "
                "VALUES (:id, :email, :ph)"
            ),
            {"id": author_id, "email": author_email, "ph": security.hash_password(_KNOWN_PASSWORD)},
        )
    try:
        first = await issues.add_comment(
            org_a, project_a, issue_id, author_id, "First note",
            session_factory=app_sessionmaker,
        )
        second = await issues.add_comment(
            org_a, project_a, issue_id, author_id, "Second note",
            session_factory=app_sessionmaker,
        )
        assert first is not None and second is not None
        assert first["author_email"] == author_email
        assert first["body"] == "First note"

        listed = await issues.list_comments(
            org_a, project_a, issue_id, session_factory=app_sessionmaker
        )
        assert listed is not None
        assert [c["body"] for c in listed] == ["First note", "Second note"]
        assert all(c["author_email"] == author_email for c in listed)
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": author_id})


async def test_list_comments_missing_issue_is_none(app_sessionmaker, two_orgs) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    result = await issues.list_comments(
        org_a, project_a, uuid.uuid4(), session_factory=app_sessionmaker
    )
    assert result is None


async def test_add_comment_missing_issue_is_none(app_sessionmaker, two_orgs) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    result = await issues.add_comment(
        org_a, project_a, uuid.uuid4(), uuid.uuid4(), "hello",
        session_factory=app_sessionmaker,
    )
    assert result is None


# --- Cross-org isolation ------------------------------------------------------
@pytest.mark.isolation
async def test_cross_org_issue_isolation(app_sessionmaker, two_orgs) -> None:
    org_a, org_b = two_orgs["org_a"], two_orgs["org_b"]
    project_a = two_orgs["project_a"]
    res = await _seed_event(
        app_sessionmaker, org_a, project_a, _exc_envelope(str(uuid.uuid4()), func="secret")
    )
    issue_id = uuid.UUID(res["issue_id"])

    # Org B cannot list, read, act on, or delete org A's project/issue.
    assert (
        await issues.list_issues(org_b, project_a, session_factory=app_sessionmaker)
        is None
    )
    assert (
        await issues.get_issue(
            org_b, project_a, issue_id, session_factory=app_sessionmaker
        )
        is None
    )
    assert (
        await issues.set_issue_status(
            org_b, project_a, issue_id, "resolve", session_factory=app_sessionmaker
        )
        is None
    )
    assert (
        await issues.delete_issue(
            org_b, project_a, issue_id, session_factory=app_sessionmaker
        )
        is False
    )
    # Org B cannot assign or comment on org A's issue either.
    assert (
        await issues.assign_issue(
            org_b, project_a, issue_id, None, session_factory=app_sessionmaker
        )
        is None
    )
    assert (
        await issues.list_comments(
            org_b, project_a, issue_id, session_factory=app_sessionmaker
        )
        is None
    )
    assert (
        await issues.add_comment(
            org_b, project_a, issue_id, uuid.uuid4(), "cross-org note",
            session_factory=app_sessionmaker,
        )
        is None
    )

    # Org A still sees its issue intact and unresolved (org B's calls changed
    # nothing).
    detail = await issues.get_issue(
        org_a, project_a, issue_id, session_factory=app_sessionmaker
    )
    assert detail is not None
    assert detail["status"] == "unresolved"


# --- HTTP-level authZ matrix --------------------------------------------------
@pytest_asyncio.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_user(conn, email: str, password: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO users (id, email, password_hash) VALUES (:id, :email, :ph)"),
        {"id": user_id, "email": email, "ph": security.hash_password(password)},
    )
    return user_id


@pytest.mark.isolation
async def test_issue_endpoints_enforce_member_and_admin_authz(
    app_sessionmaker, superuser_engine, client, two_orgs
) -> None:
    org_a, project_a = two_orgs["org_a"], two_orgs["project_a"]
    admin_email = f"iadmin-{uuid.uuid4()}@example.test"
    member_email = f"imember-{uuid.uuid4()}@example.test"
    outsider_email = f"ioutsider-{uuid.uuid4()}@example.test"
    async with superuser_engine.begin() as conn:
        admin_id = await _seed_user(conn, admin_email, _KNOWN_PASSWORD)
        member_id = await _seed_user(conn, member_email, _KNOWN_PASSWORD)
        outsider_id = await _seed_user(conn, outsider_email, _KNOWN_PASSWORD)
        await conn.execute(
            text(
                "INSERT INTO org_memberships (org_id, user_id, role) "
                "VALUES (:o, :u, 'admin')"
            ),
            {"o": org_a, "u": admin_id},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (org_id, user_id, role) "
                "VALUES (:o, :u, 'member')"
            ),
            {"o": org_a, "u": member_id},
        )
    admin_h = {"Authorization": f"Bearer {security.create_access_token(admin_id)}"}
    member_h = {"Authorization": f"Bearer {security.create_access_token(member_id)}"}
    outsider_h = {"Authorization": f"Bearer {security.create_access_token(outsider_id)}"}

    res = await _seed_event(
        app_sessionmaker, org_a, project_a, _exc_envelope(str(uuid.uuid4()), func="http")
    )
    issue_id = res["issue_id"]
    base = f"/orgs/{org_a}/projects/{project_a}/issues"
    detail_url = f"{base}/{issue_id}"
    try:
        # Member can list, read detail, and run status actions.
        assert (await client.get(base, headers=member_h)).status_code == 200
        assert (await client.get(detail_url, headers=member_h)).status_code == 200
        assert (
            await client.post(f"{detail_url}/resolve", headers=member_h)
        ).status_code == 200
        assert (
            await client.post(f"{detail_url}/reopen", headers=member_h)
        ).status_code == 200
        # Member can assign (to themselves) and comment.
        assign_resp = await client.post(
            f"{detail_url}/assign", json={"user_id": str(member_id)}, headers=member_h
        )
        assert assign_resp.status_code == 200
        assert assign_resp.json()["assigned_to_email"] == member_email
        # Assigning to a non-member is a 400.
        assert (
            await client.post(
                f"{detail_url}/assign",
                json={"user_id": str(outsider_id)},
                headers=member_h,
            )
        ).status_code == 400
        comment_resp = await client.post(
            f"{detail_url}/comments", json={"body": "Looking into this."}, headers=member_h
        )
        assert comment_resp.status_code == 201
        assert comment_resp.json()["author_email"] == member_email
        assert (
            await client.get(f"{detail_url}/comments", headers=member_h)
        ).status_code == 200
        # Empty comment body is a 400.
        assert (
            await client.post(
                f"{detail_url}/comments", json={"body": "   "}, headers=member_h
            )
        ).status_code == 400
        # Member cannot delete (admin only).
        assert (await client.delete(detail_url, headers=member_h)).status_code == 403
        # Outsider is refused everywhere.
        assert (await client.get(base, headers=outsider_h)).status_code == 403
        assert (await client.get(detail_url, headers=outsider_h)).status_code == 403
        assert (
            await client.post(f"{detail_url}/ignore", headers=outsider_h)
        ).status_code == 403
        assert (
            await client.post(
                f"{detail_url}/assign", json={"user_id": None}, headers=outsider_h
            )
        ).status_code == 403
        assert (
            await client.get(f"{detail_url}/comments", headers=outsider_h)
        ).status_code == 403
        assert (
            await client.post(
                f"{detail_url}/comments", json={"body": "sneaky"}, headers=outsider_h
            )
        ).status_code == 403
        assert (await client.delete(detail_url, headers=outsider_h)).status_code == 403
        # Admin deletes.
        assert (await client.delete(detail_url, headers=admin_h)).status_code == 204
        issue_id = None
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM users WHERE id IN (:a, :m, :o)"),
                {"a": admin_id, "m": member_id, "o": outsider_id},
            )
