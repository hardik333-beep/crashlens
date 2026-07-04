"""Unit tests for the pure logic in the issues slice (no database).

Covers occurrence zero-fill, the list-parameter validators (status filter, sort,
page, per_page clamping), the comment body-length validator, and the
unauthenticated short circuits on the issue endpoints that reject before any
database access is required.
"""

import datetime
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app import issues
from app.main import create_app


# --- Zero-fill occurrences ----------------------------------------------------
def test_zero_fill_returns_dense_window_oldest_first() -> None:
    today = datetime.date(2026, 7, 4)
    counts = {datetime.date(2026, 7, 4): 3, datetime.date(2026, 7, 1): 5}
    filled = issues.zero_fill_occurrences(counts, today)

    assert len(filled) == issues.OCCURRENCE_WINDOW_DAYS
    # Oldest first, contiguous, last entry is today.
    assert filled[0]["day"] == "2026-06-21"
    assert filled[-1]["day"] == "2026-07-04"
    days = [entry["day"] for entry in filled]
    assert days == sorted(days)
    # Known counts land on the right day; the rest are zero-filled.
    by_day = {entry["day"]: entry["count"] for entry in filled}
    assert by_day["2026-07-04"] == 3
    assert by_day["2026-07-01"] == 5
    assert by_day["2026-06-30"] == 0


def test_zero_fill_total_matches_sum_of_input() -> None:
    # COHERENCE: the UI total is the sum of THIS array, so the array's sum must
    # equal the true total of the counted days inside the window.
    today = datetime.date(2026, 7, 4)
    counts = {
        datetime.date(2026, 7, 4): 2,
        datetime.date(2026, 6, 25): 7,
        datetime.date(2026, 6, 21): 1,
    }
    filled = issues.zero_fill_occurrences(counts, today)
    assert sum(entry["count"] for entry in filled) == 10


def test_zero_fill_all_empty_is_all_zero() -> None:
    filled = issues.zero_fill_occurrences({}, datetime.date(2026, 7, 4))
    assert len(filled) == issues.OCCURRENCE_WINDOW_DAYS
    assert all(entry["count"] == 0 for entry in filled)


def test_zero_fill_excludes_days_outside_window() -> None:
    today = datetime.date(2026, 7, 4)
    # A day 20 days ago is outside the 14-day window and must not appear.
    counts = {datetime.date(2026, 6, 14): 9}
    filled = issues.zero_fill_occurrences(counts, today)
    assert all(entry["day"] != "2026-06-14" for entry in filled)
    assert sum(entry["count"] for entry in filled) == 0


# --- Status filter validation -------------------------------------------------
def test_status_filter_defaults_when_missing() -> None:
    assert issues.normalize_status_filter(None) == "unresolved"
    assert issues.normalize_status_filter("") == "unresolved"


def test_status_filter_accepts_every_valid_value() -> None:
    for value in ("unresolved", "resolved", "ignored", "regressed", "all"):
        assert issues.normalize_status_filter(value) == value


def test_status_filter_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        issues.normalize_status_filter("bogus")


# --- Sort validation ----------------------------------------------------------
def test_sort_defaults_and_accepts_valid() -> None:
    assert issues.normalize_sort(None) == "last_seen"
    for value in ("last_seen", "first_seen", "count"):
        assert issues.normalize_sort(value) == value


def test_sort_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        issues.normalize_sort("severity")


# --- Pagination clamping ------------------------------------------------------
def test_clamp_page_floors_below_one() -> None:
    assert issues.clamp_page(None) == 1
    assert issues.clamp_page(0) == 1
    assert issues.clamp_page(-5) == 1
    assert issues.clamp_page(3) == 3


def test_clamp_per_page_bounds() -> None:
    assert issues.clamp_per_page(None) == issues.DEFAULT_PER_PAGE
    assert issues.clamp_per_page(0) == issues.DEFAULT_PER_PAGE
    assert issues.clamp_per_page(10) == 10
    assert issues.clamp_per_page(1000) == issues.MAX_PER_PAGE


# --- Payload coercion ---------------------------------------------------------
def test_coerce_payload_decodes_json_string() -> None:
    assert issues._coerce_payload('{"a": 1}') == {"a": 1}


def test_coerce_payload_passes_through_dict() -> None:
    obj = {"a": 1}
    assert issues._coerce_payload(obj) is obj


def test_coerce_payload_returns_undecodable_string_unchanged() -> None:
    assert issues._coerce_payload("not json") == "not json"


# --- Comment shaping: deleted author --------------------------------------------
def test_comment_dict_null_author_surfaces_none_fields() -> None:
    """A NULL author (user deleted; FK ON DELETE SET NULL, coherent since
    revision 0004) must surface author_id and author_email as None, never the
    string "None". The dashboard renders this as "Former teammate"."""

    class _Row:
        id = uuid.uuid4()
        author = None
        body = "left behind"
        created_at = datetime.datetime(2026, 7, 4, 12, 0, tzinfo=datetime.UTC)

    shaped = issues._comment_dict(_Row(), None)
    assert shaped["author_id"] is None
    assert shaped["author_email"] is None
    assert shaped["body"] == "left behind"


def test_comment_dict_present_author_is_stringified() -> None:
    author_id = uuid.uuid4()

    class _Row:
        id = uuid.uuid4()
        author = author_id
        body = "still here"
        created_at = datetime.datetime(2026, 7, 4, 12, 0, tzinfo=datetime.UTC)

    shaped = issues._comment_dict(_Row(), "member@example.test")
    assert shaped["author_id"] == str(author_id)
    assert shaped["author_email"] == "member@example.test"


# --- Comment body-length validation --------------------------------------------
def test_comment_body_accepts_minimum_length() -> None:
    assert issues.validate_comment_body("a") == "a"


def test_comment_body_accepts_maximum_length() -> None:
    body = "a" * issues.COMMENT_BODY_MAX_LENGTH
    assert issues.validate_comment_body(body) == body


def test_comment_body_rejects_empty() -> None:
    with pytest.raises(ValueError):
        issues.validate_comment_body("")


def test_comment_body_rejects_whitespace_only() -> None:
    # Trimmed length is what is checked, so whitespace-only is rejected as empty.
    with pytest.raises(ValueError):
        issues.validate_comment_body("   \n\t  ")


def test_comment_body_rejects_over_maximum() -> None:
    with pytest.raises(ValueError):
        issues.validate_comment_body("a" * (issues.COMMENT_BODY_MAX_LENGTH + 1))


def test_comment_body_trims_surrounding_whitespace() -> None:
    assert issues.validate_comment_body("  hello world  ") == "hello world"


# --- Unauthenticated access short circuits before the database ----------------
async def test_list_issues_without_token_is_401() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/orgs/{uuid.uuid4()}/projects/{uuid.uuid4()}/issues"
        )
    assert response.status_code == 401


async def test_issue_detail_without_token_is_401() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/orgs/{uuid.uuid4()}/projects/{uuid.uuid4()}/issues/{uuid.uuid4()}"
        )
    assert response.status_code == 401


async def test_resolve_issue_without_token_is_401() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/orgs/{uuid.uuid4()}/projects/{uuid.uuid4()}/issues/{uuid.uuid4()}/resolve"
        )
    assert response.status_code == 401


async def test_delete_issue_without_token_is_401() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            f"/orgs/{uuid.uuid4()}/projects/{uuid.uuid4()}/issues/{uuid.uuid4()}"
        )
    assert response.status_code == 401


async def test_assign_issue_without_token_is_401() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/orgs/{uuid.uuid4()}/projects/{uuid.uuid4()}/issues/{uuid.uuid4()}/assign",
            json={"user_id": None},
        )
    assert response.status_code == 401


async def test_list_comments_without_token_is_401() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/orgs/{uuid.uuid4()}/projects/{uuid.uuid4()}/issues/{uuid.uuid4()}/comments"
        )
    assert response.status_code == 401


async def test_create_comment_without_token_is_401() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/orgs/{uuid.uuid4()}/projects/{uuid.uuid4()}/issues/{uuid.uuid4()}/comments",
            json={"body": "hello"},
        )
    assert response.status_code == 401
