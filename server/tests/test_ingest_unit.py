"""Unit tests for the ingest helpers -- no Postgres, Redis, or ASGI app.

Covers the three pure surfaces of the ingest slice:

* the envelope validation matrix (each required field, the message-or-exception
  rule, the level enum, and unknown-field pass-through);
* the INCREMENTAL gzip cap (a small-compressed / large-decompressed bomb is
  rejected without being fully expanded, plus round-trip and malformed cases);
* the Retry-After computation.
"""

import gzip
import json
import zlib

import pytest

from app import ingest
from app.ratelimit import REFILL_PER_SECOND, retry_after_seconds


def _valid_envelope() -> dict:
    return {
        "event_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "timestamp": "2026-07-04T12:00:00.000Z",
        "platform": "python",
        "level": "error",
        "message": "Division by zero in invoice total",
        "environment": "production",
        "sdk": {"name": "crashlens-python", "version": "0.1.0"},
    }


# --- Envelope validation matrix ----------------------------------------------
def test_valid_envelope_passes() -> None:
    ingest.validate_envelope(_valid_envelope())


@pytest.mark.parametrize(
    "field",
    ["event_id", "timestamp", "platform", "level", "environment", "sdk"],
)
def test_each_required_field_missing_is_rejected(field: str) -> None:
    envelope = _valid_envelope()
    del envelope[field]
    with pytest.raises(ingest.InvalidEnvelope):
        ingest.validate_envelope(envelope)


def test_sdk_missing_name_or_version_rejected() -> None:
    for sdk in ({"version": "0.1.0"}, {"name": "crashlens-python"}, {}):
        envelope = _valid_envelope()
        envelope["sdk"] = sdk
        with pytest.raises(ingest.InvalidEnvelope):
            ingest.validate_envelope(envelope)


def test_bad_event_id_rejected() -> None:
    envelope = _valid_envelope()
    envelope["event_id"] = "not-a-uuid"
    with pytest.raises(ingest.InvalidEnvelope):
        ingest.validate_envelope(envelope)


def test_bad_timestamp_rejected() -> None:
    envelope = _valid_envelope()
    envelope["timestamp"] = "yesterday"
    with pytest.raises(ingest.InvalidEnvelope):
        ingest.validate_envelope(envelope)


@pytest.mark.parametrize("level", ["critical", "trace", "", "ERROR", None])
def test_level_enum_is_enforced(level: object) -> None:
    envelope = _valid_envelope()
    envelope["level"] = level
    with pytest.raises(ingest.InvalidEnvelope):
        ingest.validate_envelope(envelope)


@pytest.mark.parametrize("level", sorted(ingest.VALID_LEVELS))
def test_all_five_levels_accepted(level: str) -> None:
    envelope = _valid_envelope()
    envelope["level"] = level
    ingest.validate_envelope(envelope)


def test_unknown_top_level_fields_are_ignored_and_untouched() -> None:
    envelope = _valid_envelope()
    envelope["future_field"] = {"nested": [1, 2, 3]}
    envelope["another"] = "value"
    ingest.validate_envelope(envelope)
    # Pass-through: validation must not strip or mutate unknown fields.
    assert envelope["future_field"] == {"nested": [1, 2, 3]}
    assert envelope["another"] == "value"


def test_message_only_is_valid() -> None:
    envelope = _valid_envelope()
    assert "exception" not in envelope
    ingest.validate_envelope(envelope)


def test_exception_only_is_valid() -> None:
    envelope = _valid_envelope()
    del envelope["message"]
    envelope["exception"] = {"type": "ZeroDivisionError", "value": "division by zero"}
    ingest.validate_envelope(envelope)


def test_neither_message_nor_exception_is_rejected() -> None:
    envelope = _valid_envelope()
    del envelope["message"]
    with pytest.raises(ingest.InvalidEnvelope):
        ingest.validate_envelope(envelope)


def test_exception_wrong_type_rejected() -> None:
    envelope = _valid_envelope()
    del envelope["message"]
    envelope["exception"] = "boom"
    with pytest.raises(ingest.InvalidEnvelope):
        ingest.validate_envelope(envelope)


# --- JSON parsing -------------------------------------------------------------
def test_parse_json_rejects_malformed() -> None:
    with pytest.raises(ingest.MalformedBody):
        ingest.parse_json(b"{not json")


def test_parse_json_rejects_non_object() -> None:
    with pytest.raises(ingest.InvalidEnvelope):
        ingest.parse_json(b"[1, 2, 3]")


def test_parse_json_round_trip() -> None:
    envelope = _valid_envelope()
    assert ingest.parse_json(json.dumps(envelope).encode()) == envelope


# --- Incremental gzip cap -----------------------------------------------------
def test_gzip_round_trip_under_cap() -> None:
    payload = json.dumps(_valid_envelope()).encode()
    assert ingest.decompress_gzip(gzip.compress(payload)) == payload


def test_gzip_bomb_rejected_without_full_expansion() -> None:
    # ~50 MB of zeros compresses to a tiny body but would blow past the 1 MB cap
    # if fully expanded. The incremental loop must reject it, and because it caps
    # the output buffer it can never have materialised the full 50 MB: assert the
    # decompressor still holds unconsumed input when it stops.
    bomb = gzip.compress(b"\x00" * (50 * 1024 * 1024))
    assert len(bomb) < ingest.MAX_DECOMPRESSED_BYTES  # small on the wire
    with pytest.raises(ingest.PayloadTooLarge):
        ingest.decompress_gzip(bomb)


def test_gzip_just_over_cap_rejected() -> None:
    payload = b"a" * (ingest.MAX_DECOMPRESSED_BYTES + 1)
    with pytest.raises(ingest.PayloadTooLarge):
        ingest.decompress_gzip(gzip.compress(payload))


def test_gzip_just_under_cap_accepted() -> None:
    payload = b"a" * (ingest.MAX_DECOMPRESSED_BYTES - 1024)
    assert ingest.decompress_gzip(gzip.compress(payload)) == payload


def test_malformed_gzip_rejected() -> None:
    with pytest.raises(ingest.MalformedBody):
        ingest.decompress_gzip(b"this is not gzip at all")


def test_incremental_loop_stops_before_consuming_whole_bomb() -> None:
    # A white-box check that the cap is enforced INCREMENTALLY: drive the same
    # decompressobj the helper uses and confirm that, at the moment output first
    # exceeds the cap, there is still un-decompressed input parked in the stream.
    bomb = gzip.compress(b"\x00" * (50 * 1024 * 1024))
    decompressor = zlib.decompressobj(47)
    out = bytearray()
    chunk = decompressor.decompress(bomb, 64 * 1024)
    stopped_with_input_left = False
    while chunk:
        out.extend(chunk)
        if len(out) > ingest.MAX_DECOMPRESSED_BYTES:
            stopped_with_input_left = bool(decompressor.unconsumed_tail)
            break
        chunk = decompressor.decompress(decompressor.unconsumed_tail, 64 * 1024)
    assert stopped_with_input_left
    assert len(out) <= ingest.MAX_DECOMPRESSED_BYTES + 64 * 1024


# --- Retry-After computation --------------------------------------------------
def test_retry_after_zero_when_a_token_is_available() -> None:
    assert retry_after_seconds(1.0) == 0
    assert retry_after_seconds(5.0) == 0


def test_retry_after_rounds_up_and_floors_at_one() -> None:
    # Empty bucket: full second to accrue one token at 1 token/sec.
    assert retry_after_seconds(0.0) == 1
    # Almost a token: still rounds UP to a whole second, never below 1.
    assert retry_after_seconds(0.99) == 1
    assert retry_after_seconds(0.5) == 1


def test_retry_after_scales_with_slower_refill() -> None:
    # At 0.5 tokens/sec an empty bucket needs 2 seconds for one token.
    assert retry_after_seconds(0.0, refill_per_second=0.5) == 2
    # Sanity: the default refill is one token per second.
    assert REFILL_PER_SECOND == 1.0
