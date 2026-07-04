"""Unit tests for the pure logic in the instance-admin panel (no database).

Covers the last-admin orphan guard (the only non-trivial branch that is safe to
test without Postgres) and the pagination clamp helpers, mirroring the audit
slice's unit-test discipline.
"""

import pytest

from app import admin


# --- Last-admin orphan guard ---------------------------------------------------
def test_removing_own_flag_as_last_admin_is_blocked() -> None:
    assert admin.would_orphan_instance(enabled=False, is_self=True, admin_count=1) is True


def test_removing_own_flag_with_another_admin_present_is_allowed() -> None:
    assert admin.would_orphan_instance(enabled=False, is_self=True, admin_count=2) is False


def test_removing_someone_elses_flag_is_never_an_orphan() -> None:
    # The caller (an instance admin) remains, so removing another admin cannot
    # orphan the instance regardless of the count.
    assert admin.would_orphan_instance(enabled=False, is_self=False, admin_count=1) is False
    assert admin.would_orphan_instance(enabled=False, is_self=False, admin_count=2) is False


def test_granting_the_flag_never_orphans() -> None:
    assert admin.would_orphan_instance(enabled=True, is_self=True, admin_count=1) is False
    assert admin.would_orphan_instance(enabled=True, is_self=False, admin_count=0) is False


# --- Pagination clamps ---------------------------------------------------------
@pytest.mark.parametrize("value,expected", [(None, 1), (0, 1), (-5, 1), (1, 1), (9, 9)])
def test_clamp_page(value, expected) -> None:
    assert admin.clamp_page(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, admin.DEFAULT_PER_PAGE),
        (0, admin.DEFAULT_PER_PAGE),
        (-1, admin.DEFAULT_PER_PAGE),
        (1, 1),
        (50, 50),
        (admin.MAX_PER_PAGE, admin.MAX_PER_PAGE),
        (admin.MAX_PER_PAGE + 25, admin.MAX_PER_PAGE),
    ],
)
def test_clamp_per_page(value, expected) -> None:
    assert admin.clamp_per_page(value) == expected


# --- Queue key is arq's own constant, not a guessed string ---------------------
def test_queue_key_is_arqs_default_queue_name() -> None:
    from arq.constants import default_queue_name

    assert admin._QUEUE_KEY == default_queue_name
