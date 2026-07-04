"""Unit tests for the process_event pure functions -- no Postgres, Redis, or app.

Covers the truncation matrix (each protocol rule), fingerprint stability and
discrimination (same trace -> same hash, different function -> different, digit/
uuid collapse, root-cause selection through a cause chain, >5-deep truncation),
and title derivation.
"""

import copy

from app.jobs.process_event import (
    CONTEXT_LINE_MAX,
    FILENAME_MAX,
    FUNCTION_MAX,
    MAX_BREADCRUMBS,
    MAX_CAUSE_DEPTH,
    MAX_FRAMES,
    MESSAGE_MAX,
    TAG_KEY_MAX,
    TAG_VALUE_MAX,
    TITLE_MAX,
    TRUNCATION_MARKER,
    compute_fingerprint,
    derive_title,
    normalize_envelope,
    normalize_message,
    truncate_string,
    walk_to_root_cause,
)


def _exc_envelope(frames=None, exc_type="ZeroDivisionError", value="division by zero"):
    if frames is None:
        frames = [
            {"filename": "app/billing/invoice.py", "function": "compute_total", "in_app": True},
        ]
    return {
        "event_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "timestamp": "2026-07-04T12:00:00.000Z",
        "platform": "python",
        "level": "error",
        "environment": "production",
        "sdk": {"name": "crashlens-python", "version": "0.1.0"},
        "exception": {"type": exc_type, "value": value, "stacktrace": {"frames": frames}},
    }


def _msg_envelope(message="user 123 not found", level="error", platform="python"):
    return {
        "event_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "timestamp": "2026-07-04T12:00:00.000Z",
        "platform": platform,
        "level": level,
        "environment": "production",
        "sdk": {"name": "crashlens-python", "version": "0.1.0"},
        "message": message,
    }


# --- truncate_string ----------------------------------------------------------
def test_truncate_string_leaves_short_values_untouched() -> None:
    assert truncate_string("short", 100) == "short"
    assert truncate_string("exact", 5) == "exact"


def test_truncate_string_caps_and_appends_marker_within_limit() -> None:
    result = truncate_string("a" * 500, 100)
    assert len(result) == 100
    assert result.endswith(TRUNCATION_MARKER)
    assert result == "a" * 97 + TRUNCATION_MARKER


def test_truncate_string_passes_through_non_strings() -> None:
    assert truncate_string(None, 10) is None  # type: ignore[arg-type]
    assert truncate_string(42, 10) == 42  # type: ignore[arg-type]


# --- normalize_envelope: the truncation matrix (one rule per test) -----------
def test_message_truncated_to_cap() -> None:
    env = _msg_envelope(message="x" * (MESSAGE_MAX + 500))
    out = normalize_envelope(env)
    assert len(out["message"]) == MESSAGE_MAX
    assert out["message"].endswith(TRUNCATION_MARKER)


def test_tag_keys_and_values_truncated() -> None:
    env = _msg_envelope()
    env["tags"] = {"k" * (TAG_KEY_MAX + 10): "v" * (TAG_VALUE_MAX + 10)}
    out = normalize_envelope(env)
    (key, value), = out["tags"].items()
    assert len(key) == TAG_KEY_MAX
    assert len(value) == TAG_VALUE_MAX


def test_filename_and_function_truncated() -> None:
    env = _exc_envelope(
        frames=[{"filename": "f" * (FILENAME_MAX + 50), "function": "g" * (FUNCTION_MAX + 50)}]
    )
    out = normalize_envelope(env)
    frame = out["exception"]["stacktrace"]["frames"][0]
    assert len(frame["filename"]) == FILENAME_MAX
    assert len(frame["function"]) == FUNCTION_MAX


def test_context_lines_truncated() -> None:
    env = _exc_envelope(
        frames=[
            {
                "filename": "a.py",
                "function": "f",
                "context_line": "c" * (CONTEXT_LINE_MAX + 20),
                "pre_context": ["p" * (CONTEXT_LINE_MAX + 20)],
                "post_context": ["q" * (CONTEXT_LINE_MAX + 20)],
            }
        ]
    )
    frame = normalize_envelope(env)["exception"]["stacktrace"]["frames"][0]
    assert len(frame["context_line"]) == CONTEXT_LINE_MAX
    assert len(frame["pre_context"][0]) == CONTEXT_LINE_MAX
    assert len(frame["post_context"][0]) == CONTEXT_LINE_MAX


def test_breadcrumbs_keep_newest_100() -> None:
    env = _msg_envelope()
    env["breadcrumbs"] = [{"message": str(i)} for i in range(MAX_BREADCRUMBS + 40)]
    out = normalize_envelope(env)
    assert len(out["breadcrumbs"]) == MAX_BREADCRUMBS
    # Newest kept: last element is the highest index, oldest dropped.
    assert out["breadcrumbs"][-1]["message"] == str(MAX_BREADCRUMBS + 39)
    assert out["breadcrumbs"][0]["message"] == str(40)


def test_frames_keep_last_128() -> None:
    frames = [{"filename": f"f{i}.py", "function": f"fn{i}"} for i in range(MAX_FRAMES + 30)]
    out = normalize_envelope(_exc_envelope(frames=frames))
    kept = out["exception"]["stacktrace"]["frames"]
    assert len(kept) == MAX_FRAMES
    # LAST 128 (nearest the crash) kept: the final frame survives, earliest drop.
    assert kept[-1]["function"] == f"fn{MAX_FRAMES + 29}"
    assert kept[0]["function"] == "fn30"


def test_cause_chain_truncated_beyond_max_depth() -> None:
    # Build a 7-deep chain; only 5 should survive (cause dropped on the 5th).
    exc = {"type": "E5", "value": "v5", "stacktrace": {"frames": []}}
    for depth in range(4, -1, -1):
        exc = {
            "type": f"E{depth}",
            "value": f"v{depth}",
            "stacktrace": {"frames": []},
            "cause": exc,
        }
    # exc is now E0 -> E1 -> ... a 6-link chain; extend one deeper to be sure.
    env = _msg_envelope()
    del env["message"]
    env["exception"] = exc
    out = normalize_envelope(env)
    # Walk and count surviving exceptions.
    depth = 1
    node = out["exception"]
    while isinstance(node.get("cause"), dict):
        node = node["cause"]
        depth += 1
    assert depth == MAX_CAUSE_DEPTH
    assert "cause" not in node


def test_normalize_does_not_mutate_input_and_preserves_unknown_fields() -> None:
    env = _msg_envelope(message="m")
    env["future_field"] = {"nested": [1, 2, 3]}
    before = copy.deepcopy(env)
    out = normalize_envelope(env)
    assert env == before  # input untouched
    assert out["future_field"] == {"nested": [1, 2, 3]}  # unknown field preserved


# --- normalize_message --------------------------------------------------------
def test_normalize_message_collapses_digits() -> None:
    assert normalize_message("user 123 not found") == "user <n> not found"
    assert normalize_message("user 456 not found") == "user <n> not found"


def test_normalize_message_collapses_uuid_and_hex() -> None:
    assert (
        normalize_message("row 3fa85f64-5717-4562-b3fc-2c963f66afa6 gone")
        == "row <uuid> gone"
    )
    assert normalize_message("ptr 0xDEADBEEF") == "ptr <hex>"
    assert normalize_message("hash deadbeefdeadbeef99") == "hash <hex>"


# --- compute_fingerprint: stability + discrimination -------------------------
def test_same_trace_same_fingerprint() -> None:
    a = normalize_envelope(_exc_envelope())
    b = normalize_envelope(_exc_envelope())
    assert compute_fingerprint(a) == compute_fingerprint(b)


def test_different_function_different_fingerprint() -> None:
    a = normalize_envelope(_exc_envelope(frames=[{"filename": "a.py", "function": "foo"}]))
    b = normalize_envelope(_exc_envelope(frames=[{"filename": "a.py", "function": "bar"}]))
    assert compute_fingerprint(a) != compute_fingerprint(b)


def test_digit_only_message_groups_together() -> None:
    a = normalize_envelope(_msg_envelope(message="user 123 not found"))
    b = normalize_envelope(_msg_envelope(message="user 456 not found"))
    assert compute_fingerprint(a) == compute_fingerprint(b)


def test_platform_is_part_of_fingerprint() -> None:
    a = normalize_envelope(_msg_envelope(platform="python"))
    b = normalize_envelope(_msg_envelope(platform="node"))
    assert compute_fingerprint(a) != compute_fingerprint(b)


def test_exception_and_message_fingerprints_differ() -> None:
    exc = normalize_envelope(_exc_envelope())
    msg = normalize_envelope(_msg_envelope())
    assert compute_fingerprint(exc) != compute_fingerprint(msg)


def test_fingerprint_uses_root_cause_through_three_deep_chain() -> None:
    # Two envelopes with the SAME root cause but DIFFERENT outer exceptions must
    # fingerprint identically -- proving the root cause drives grouping.
    root = {
        "type": "PermissionError",
        "value": "denied",
        "stacktrace": {"frames": [{"filename": "auth.py", "function": "check", "in_app": True}]},
    }
    mid = {"type": "ValueError", "value": "bad", "stacktrace": {"frames": []}, "cause": root}

    env_a = _msg_envelope()
    del env_a["message"]
    env_a["exception"] = {"type": "RuntimeError", "value": "outer A",
                          "stacktrace": {"frames": []}, "cause": copy.deepcopy(mid)}

    env_b = _msg_envelope()
    del env_b["message"]
    env_b["exception"] = {"type": "SystemError", "value": "outer B",
                          "stacktrace": {"frames": []}, "cause": copy.deepcopy(mid)}

    fa = compute_fingerprint(normalize_envelope(env_a))
    fb = compute_fingerprint(normalize_envelope(env_b))
    assert fa == fb


def test_different_root_cause_discriminates() -> None:
    root_a = {"type": "PermissionError", "value": "denied",
              "stacktrace": {"frames": [{"filename": "auth.py", "function": "check"}]}}
    root_b = {"type": "KeyError", "value": "missing",
              "stacktrace": {"frames": [{"filename": "auth.py", "function": "check"}]}}
    env_a = _msg_envelope()
    del env_a["message"]
    env_b = _msg_envelope()
    del env_b["message"]
    env_a["exception"] = {"type": "RuntimeError", "value": "o",
                          "stacktrace": {"frames": []}, "cause": root_a}
    env_b["exception"] = {"type": "RuntimeError", "value": "o",
                          "stacktrace": {"frames": []}, "cause": root_b}
    assert compute_fingerprint(normalize_envelope(env_a)) != compute_fingerprint(
        normalize_envelope(env_b)
    )


def test_walk_to_root_cause_returns_deepest() -> None:
    root = {"type": "Root", "value": "r", "stacktrace": {"frames": []}}
    chain = {"type": "Top", "value": "t", "stacktrace": {"frames": []},
             "cause": {"type": "Mid", "value": "m", "stacktrace": {"frames": []}, "cause": root}}
    assert walk_to_root_cause(chain)["type"] == "Root"


def test_only_last_8_in_app_frames_feed_the_hash() -> None:
    # Frames 0..19 in_app; the first 12 differ, last 8 identical -> same hash
    # (only the last 8 in_app frames are hashed).
    tail = [{"filename": f"t{i}.py", "function": f"t{i}", "in_app": True} for i in range(8)]
    frames_a = [
        {"filename": f"a{i}.py", "function": f"a{i}", "in_app": True} for i in range(12)
    ] + tail
    frames_b = [
        {"filename": f"b{i}.py", "function": f"b{i}", "in_app": True} for i in range(12)
    ] + tail
    fa = compute_fingerprint(normalize_envelope(_exc_envelope(frames=frames_a)))
    fb = compute_fingerprint(normalize_envelope(_exc_envelope(frames=frames_b)))
    assert fa == fb


def test_library_frames_excluded_from_fingerprint() -> None:
    # An in_app frame plus a library frame should hash the same as the in_app
    # frame alone (in_app=false frames are excluded).
    with_lib = _exc_envelope(frames=[
        {"filename": "app.py", "function": "run", "in_app": True},
        {"filename": "lib.py", "function": "internal", "in_app": False},
    ])
    without_lib = _exc_envelope(frames=[
        {"filename": "app.py", "function": "run", "in_app": True},
    ])
    assert compute_fingerprint(normalize_envelope(with_lib)) == compute_fingerprint(
        normalize_envelope(without_lib)
    )


# --- derive_title -------------------------------------------------------------
def test_title_from_exception_type_and_value_first_line() -> None:
    env = normalize_envelope(
        _exc_envelope(exc_type="ZeroDivisionError", value="division by zero\nmore")
    )
    assert derive_title(env) == "ZeroDivisionError: division by zero"


def test_title_from_message_first_line_when_no_exception() -> None:
    env = normalize_envelope(_msg_envelope(message="something broke\nsecond line"))
    assert derive_title(env) == "something broke"


def test_title_capped_to_limit() -> None:
    env = normalize_envelope(_msg_envelope(message="z" * (TITLE_MAX + 100)))
    title = derive_title(env)
    assert len(title) == TITLE_MAX
    assert title.endswith(TRUNCATION_MARKER)


def test_title_uses_top_level_exception_not_root_cause() -> None:
    env = _msg_envelope()
    del env["message"]
    env["exception"] = {
        "type": "RuntimeError", "value": "outer failure",
        "stacktrace": {"frames": []},
        "cause": {"type": "ValueError", "value": "inner", "stacktrace": {"frames": []}},
    }
    assert derive_title(normalize_envelope(env)) == "RuntimeError: outer failure"
