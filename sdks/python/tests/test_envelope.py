"""Envelope conformance against docs/PROTOCOL.md."""

from __future__ import annotations

import sys

from crashlens import _envelope, _scope

REQUIRED_TOP_LEVEL = {
    "event_id",
    "timestamp",
    "platform",
    "level",
    "environment",
    "sdk",
}


def _make_exc_info():
    try:
        raise ValueError("boom")
    except ValueError:
        return sys.exc_info()


def build(**over):
    defaults = dict(
        sdk_version="0.1.0",
        environment="production",
        release=None,
        prefixes=None,
        level="error",
    )
    defaults.update(over)
    return _envelope.build_event(**defaults)


def test_required_fields_present_for_message():
    event = build(message="hello", level="info")
    assert REQUIRED_TOP_LEVEL.issubset(event.keys())
    assert event["platform"] == "python"
    assert event["sdk"] == {"name": "crashlens-python", "version": "0.1.0"}
    assert event["message"] == "hello"


def test_timestamp_is_rfc3339_utc():
    event = build(message="x")
    ts = event["timestamp"]
    assert ts.endswith("Z")
    assert "T" in ts


def test_event_id_is_uuid4():
    import uuid

    event = build(message="x")
    parsed = uuid.UUID(event["event_id"])
    assert parsed.version == 4


def test_exception_shape():
    event = build(exc_info=_make_exc_info())
    exc = event["exception"]
    assert exc["type"] == "ValueError"
    assert exc["value"] == "boom"
    assert "stacktrace" in exc
    assert isinstance(exc["stacktrace"]["frames"], list)
    assert len(exc["stacktrace"]["frames"]) >= 1


def test_frames_are_oldest_first_crash_last():
    def level_three():
        raise RuntimeError("deep")

    def level_two():
        level_three()

    def level_one():
        level_two()

    try:
        level_one()
    except RuntimeError:
        info = sys.exc_info()

    event = build(exc_info=info)
    frames = event["exception"]["stacktrace"]["frames"]
    functions = [f["function"] for f in frames]
    # Oldest (this test fn) first, crash site (level_three) last.
    assert functions[-1] == "level_three"
    assert functions.index("level_one") < functions.index("level_two")
    assert functions.index("level_two") < functions.index("level_three")


def test_frame_has_required_fields():
    event = build(exc_info=_make_exc_info())
    frame = event["exception"]["stacktrace"]["frames"][-1]
    for key in ("filename", "function", "lineno", "colno", "in_app"):
        assert key in frame
    assert isinstance(frame["lineno"], int)
    assert isinstance(frame["colno"], int)
    assert isinstance(frame["in_app"], bool)


def test_context_line_captured():
    event = build(exc_info=_make_exc_info())
    # The crash frame is inside _make_exc_info in this test module.
    frames = event["exception"]["stacktrace"]["frames"]
    crash = frames[-1]
    assert "context_line" in crash
    assert "raise ValueError" in crash["context_line"]


def test_explicit_cause_chain():
    try:
        try:
            raise ValueError("root")
        except ValueError as e:
            raise RuntimeError("wrapper") from e
    except RuntimeError:
        info = sys.exc_info()

    event = build(exc_info=info)
    exc = event["exception"]
    assert exc["type"] == "RuntimeError"
    assert exc["cause"]["type"] == "ValueError"
    assert exc["cause"]["value"] == "root"


def test_implicit_context_used_when_no_cause():
    try:
        try:
            raise ValueError("first")
        except ValueError:
            raise RuntimeError("second")  # implicit __context__
    except RuntimeError:
        info = sys.exc_info()

    event = build(exc_info=info)
    assert event["exception"]["cause"]["type"] == "ValueError"


def test_suppressed_context_not_included():
    try:
        try:
            raise ValueError("first")
        except ValueError:
            raise RuntimeError("second") from None  # suppress context
    except RuntimeError:
        info = sys.exc_info()

    event = build(exc_info=info)
    assert "cause" not in event["exception"]


def test_cause_chain_depth_capped_at_five():
    # Build a chain deeper than 5 via explicit "from".
    exc = None
    for i in range(8):
        try:
            raise ValueError(f"level-{i}") from exc
        except ValueError as e:
            exc = e
    info = (type(exc), exc, exc.__traceback__)

    event = build(exc_info=info)
    depth = 1
    node = event["exception"]
    while "cause" in node:
        node = node["cause"]
        depth += 1
    assert depth == _envelope.MAX_CAUSE_DEPTH == 5


def test_in_app_true_for_test_module():
    event = build(exc_info=_make_exc_info())
    crash = event["exception"]["stacktrace"]["frames"][-1]
    assert crash["in_app"] is True


def test_in_app_prefix_overrides_path_heuristic():
    # The Docker case: an app pip-installed into site-packages must still be
    # in_app when its module matches a provided prefix. The prefix ALONE
    # decides; the site-packages path heuristic is ignored.
    assert (
        _envelope._is_in_app(
            "/usr/local/lib/python3.12/site-packages/myapp/views.py",
            "myapp.views",
            ["myapp"],
        )
        is True
    )


def test_in_app_prefix_nonmatch_is_false_even_off_library_paths():
    # Prefix provided but the module does not match: False, even though the
    # file path is application-like (not stdlib, not site-packages).
    assert (
        _envelope._is_in_app(
            "/srv/app/other/module.py",
            "other.module",
            ["myapp"],
        )
        is False
    )


def test_in_app_no_prefixes_uses_path_heuristic():
    # Without prefixes the path heuristic decides, unchanged.
    assert (
        _envelope._is_in_app(
            "/usr/local/lib/python3.12/site-packages/requests/api.py",
            "requests.api",
            None,
        )
        is False
    )
    assert _envelope._is_in_app("/srv/app/myapp/views.py", "myapp.views", None) is True


def test_in_app_prefix_filter_excludes_nonmatching():
    event = build(exc_info=_make_exc_info(), prefixes=["some.other.package"])
    crash = event["exception"]["stacktrace"]["frames"][-1]
    # Our module name does not start with that prefix, so in_app is False.
    assert crash["in_app"] is False


def test_breadcrumbs_included_and_ordered():
    _scope.clear()
    for i in range(3):
        _scope.add_breadcrumb(f"crumb-{i}", category="test")
    event = build(message="x")
    crumbs = event["breadcrumbs"]
    assert [c["message"] for c in crumbs] == ["crumb-0", "crumb-1", "crumb-2"]


def test_breadcrumb_ring_buffer_caps_at_100():
    _scope.clear()
    for i in range(150):
        _scope.add_breadcrumb(f"crumb-{i}")
    event = build(message="x")
    crumbs = event["breadcrumbs"]
    assert len(crumbs) == 100
    # Newest kept, oldest dropped.
    assert crumbs[-1]["message"] == "crumb-149"
    assert crumbs[0]["message"] == "crumb-50"


def test_tags_and_user_included():
    _scope.clear()
    _scope.set_tag("server", "web-1")
    _scope.set_user("user-42")
    event = build(message="x")
    assert event["tags"] == {"server": "web-1"}
    assert event["user"] == {"id": "user-42"}


def test_request_included_when_provided():
    event = build(message="x", request={"url": "https://a/b", "method": "POST"})
    assert event["request"] == {"url": "https://a/b", "method": "POST"}


def test_release_included_when_set():
    event = build(message="x", release="web@1.2.3")
    assert event["release"] == "web@1.2.3"
