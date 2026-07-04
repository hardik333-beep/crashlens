"""Unit tests for the pure logic in the audit slice (no database).

Covers: the action/label mapping is complete (every canonical action has a
plain-language label and vice versa), the data-shape guard rejects
secret-shaped keys at any nesting depth and does so BEFORE any database write
is attempted, and the pagination clamp helpers.
"""

import pytest

from app import audit


# --- Action/label completeness -------------------------------------------------
def test_every_action_has_a_label() -> None:
    missing = set(audit.ACTIONS) - set(audit.ACTION_LABELS)
    assert missing == set(), f"actions with no label: {missing}"


def test_every_label_maps_to_a_known_action() -> None:
    extra = set(audit.ACTION_LABELS) - set(audit.ACTIONS)
    assert extra == set(), f"labels for unknown actions: {extra}"


def test_actions_list_has_no_duplicates() -> None:
    assert len(audit.ACTIONS) == len(set(audit.ACTIONS))


@pytest.mark.parametrize("action", audit.ACTIONS)
def test_label_is_short_plain_language(action: str) -> None:
    label = audit.ACTION_LABELS[action]
    assert label == label.strip()
    assert label != ""
    # Plain language: no dot-notation internal identifiers leaking into the copy.
    assert "." not in label


# --- Data-shape guard (secrets never enter the audit trail) --------------------
def test_check_data_safety_allows_small_identifying_facts() -> None:
    audit._check_data_safety(
        {"name": "Payments API", "slug": "payments-api-abc123", "sampling_rate": 0.5}
    )  # must not raise


@pytest.mark.parametrize(
    "bad_key",
    ["token", "Token", "webhook_url", "url", "password", "secret", "api_secret"],
)
def test_check_data_safety_rejects_secret_shaped_keys(bad_key: str) -> None:
    with pytest.raises(audit.UnsafeAuditDataError):
        audit._check_data_safety({bad_key: "whatever"})


def test_check_data_safety_rejects_nested_secret_shaped_keys() -> None:
    with pytest.raises(audit.UnsafeAuditDataError):
        audit._check_data_safety({"config": {"webhook_url": "https://example.test"}})


def test_check_data_safety_allows_masked_target_under_a_safe_key_name() -> None:
    # The masked host (via alerts.mask_target) is stored under "target", not
    # under a key that names it a url/secret -- exactly how the instrumented
    # channel actions record it.
    audit._check_data_safety({"type": "slack", "target": "https://hooks.slack.com/..."})


class _RecordingSession:
    """A fake AsyncSession that records whether ``execute`` was ever called.

    Used to prove the data-shape guard runs and raises BEFORE the INSERT is
    attempted (no real database needed for this).
    """

    def __init__(self) -> None:
        self.executed = False

    async def execute(self, *args, **kwargs):
        self.executed = True
        raise AssertionError("execute() must not run when the data guard rejects the input")


async def test_record_raises_before_executing_when_data_is_unsafe() -> None:
    session = _RecordingSession()
    with pytest.raises(audit.UnsafeAuditDataError):
        await audit.record(
            session,
            org_id="00000000-0000-0000-0000-000000000000",
            actor_user_id=None,
            action="channel.created",
            target_type="alert_channel",
            data={"webhook_url": "https://hooks.slack.com/services/leak"},
        )
    assert session.executed is False


# --- Pagination clamp helpers ---------------------------------------------------
@pytest.mark.parametrize("value,expected", [(None, 1), (0, 1), (-5, 1), (1, 1), (7, 7)])
def test_clamp_page(value, expected) -> None:
    assert audit.clamp_page(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, audit.DEFAULT_PER_PAGE),
        (0, audit.DEFAULT_PER_PAGE),
        (-1, audit.DEFAULT_PER_PAGE),
        (1, 1),
        (50, 50),
        (audit.MAX_PER_PAGE, audit.MAX_PER_PAGE),
        (audit.MAX_PER_PAGE + 50, audit.MAX_PER_PAGE),
    ],
)
def test_clamp_per_page(value, expected) -> None:
    assert audit.clamp_per_page(value) == expected
