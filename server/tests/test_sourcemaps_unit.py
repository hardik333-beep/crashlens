"""Unit tests for source map parsing, path safety, and symbolication.

Pure functions only -- no database, no HTTP -- so these run in the default
``pytest -q`` pass without Postgres. Two fixture assets live in
``tests/fixtures/sourcemaps/`` and are REAL esbuild output (``esbuild
--bundle --minify --sourcemap`` over a small ``invoice.ts``), so the VLQ
decoder and symbolication are exercised against a genuine Source Map v3
document, not a hand-rolled toy.
"""

import datetime
import os

import pytest

from app import sourcemaps

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "sourcemaps")
_MAP_PATH = os.path.join(_FIXTURE_DIR, "app.min.js.map")


# ==============================================================================
# VLQ decoder -- hand-computed fixtures.
# ==============================================================================


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        ("A", [0]),  # digit 0 -> sign 0, magnitude 0.
        ("C", [1]),  # digit 2 -> sign 0, magnitude 1.
        ("D", [-1]),  # digit 3 -> sign 1, magnitude 1.
        ("B", [0]),  # digit 1 -> sign 1, magnitude 0 (negative zero == 0).
        ("gB", [16]),  # continuation: (0)<<0 then (1)<<5 = 32 -> magnitude 16.
        ("MAAO", [6, 0, 0, 7]),  # 12->6, 0, 0, 14->7 (first segment of the fixture).
    ],
)
def test_decode_vlq_hand_computed(segment: str, expected: list[int]) -> None:
    assert sourcemaps.decode_vlq(segment) == expected


def test_decode_vlq_rejects_bad_character() -> None:
    with pytest.raises(ValueError):
        sourcemaps.decode_vlq("A!")


def test_decode_vlq_rejects_truncated_value() -> None:
    # 'g' sets the continuation bit but no group follows.
    with pytest.raises(ValueError):
        sourcemaps.decode_vlq("g")


# ==============================================================================
# Path safety.
# ==============================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("app.min.js.map", "app.min.js.map"),
        ("dist/app.min.js.map", "app.min.js.map"),  # dir component discarded.
        ("../../../etc/passwd", "passwd"),  # traversal neutralised to a basename.
        ("foo/../../bar.map", "bar.map"),
    ],
)
def test_safe_basename_accepts_and_reduces(raw: str, expected: str) -> None:
    assert sourcemaps.safe_basename(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        ".",
        "..",
        "a..b.map",  # residual '..' anywhere is rejected (hard requirement).
        "a\\b.map",  # backslash separator (defeats a Windows-style path).
        "a\x00b.map",  # embedded NUL.
    ],
)
def test_safe_basename_rejects_unsafe(raw: str) -> None:
    assert sourcemaps.safe_basename(raw) is None


def test_release_dirname_is_path_safe_and_reversible() -> None:
    for release in ["web@1.4.2", "../../../etc/passwd", "a/b\\c..d", "release with spaces"]:
        dirname = sourcemaps.release_dirname(release)
        # base64url alphabet only: no separators, dots, or traversal possible.
        assert "/" not in dirname
        assert "\\" not in dirname
        assert ".." not in dirname
        assert "." not in dirname
        # Round-trips back to the exact original string.
        assert sourcemaps.release_from_dirname(dirname) == release


def test_release_from_dirname_rejects_garbage() -> None:
    # A stray directory a human dropped in the tree must not crash the listing.
    assert sourcemaps.release_from_dirname("not!valid!base64") is None


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("https://cdn.example.com/assets/app.min.js", "app.min.js"),
        ("https://cdn.example.com/assets/app.min.js?v=3", "app.min.js"),
        ("https://cdn.example.com/assets/app.min.js#frag", "app.min.js"),
        ("app.min.js", "app.min.js"),
        ("/srv/app.min.js.map", "app.min.js.map"),
    ],
)
def test_basename_from_frame_filename(filename: str, expected: str) -> None:
    assert sourcemaps.basename_from_frame_filename(filename) == expected


@pytest.mark.parametrize("bad", ["", None, 123, "http://host/"])
def test_basename_from_frame_filename_none(bad: object) -> None:
    assert sourcemaps.basename_from_frame_filename(bad) is None


def test_map_candidates() -> None:
    assert sourcemaps.map_candidates("app.min.js") == ["app.min.js.map"]
    assert sourcemaps.map_candidates("app.min.js.map") == ["app.min.js.map"]


# ==============================================================================
# Parsing + symbolication against the real esbuild fixture.
# ==============================================================================


def test_parse_real_esbuild_map_and_lookup() -> None:
    source_map = sourcemaps.load_source_map(_MAP_PATH)
    assert source_map is not None
    assert source_map.sources == ["../src/invoice.ts"]
    # The throw is at generated line 1 (0-based 0), column 42 (0-based 41).
    resolved = source_map.lookup(0, 41)
    assert resolved is not None
    src_index, orig_line, _orig_col, _name = resolved
    assert src_index == 0
    assert orig_line == 3  # 0-based -> original line 4, the throw statement.


def test_symbolicate_envelope_rewrites_frame() -> None:
    # Point the release dir at the fixture directory by encoding a release whose
    # dirname equals the fixture folder name is not possible; instead build a temp
    # layout in the integration test. Here we exercise the frame rewrite directly
    # against the parsed map via a crafted release tree using the fixture file.
    source_map = sourcemaps.load_source_map(_MAP_PATH)
    assert source_map is not None
    frame = {
        "filename": "https://cdn.example.com/app.min.js",
        "function": "t",
        "lineno": 1,
        "colno": 42,
        "in_app": True,
    }
    out = sourcemaps._symbolicate_frame(frame, source_map)
    assert out["filename"] == "../src/invoice.ts"
    assert out["lineno"] == 4
    assert out["colno"] == 5
    # Minified values are preserved.
    assert out["raw_filename"] == "https://cdn.example.com/app.min.js"
    assert out["raw_lineno"] == 1
    # Context comes from the embedded sourcesContent.
    assert "throw new Error" in out["context_line"]
    assert len(out["pre_context"]) <= sourcemaps.CONTEXT_WINDOW
    assert len(out["post_context"]) <= sourcemaps.CONTEXT_WINDOW


def test_symbolicate_frame_out_of_range_is_noop() -> None:
    source_map = sourcemaps.load_source_map(_MAP_PATH)
    assert source_map is not None
    frame = {"filename": "x", "function": "f", "lineno": 999, "colno": 1}
    assert sourcemaps._symbolicate_frame(frame, source_map) == frame


def test_symbolicate_envelope_end_to_end(tmp_path) -> None:
    """A full envelope symbolicates through a real on-disk release tree."""
    org_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    release = "web@1.4.2"
    release_dir = sourcemaps.release_maps_dir(str(tmp_path), org_id, project_id, release)
    os.makedirs(release_dir)
    with open(_MAP_PATH, "rb") as handle:
        data = handle.read()
    with open(os.path.join(release_dir, "app.min.js.map"), "wb") as handle:
        handle.write(data)

    envelope = {
        "event_id": "e",
        "platform": "javascript",
        "release": release,
        "exception": {
            "type": "Error",
            "value": "Division by zero in invoice total",
            "stacktrace": {
                "frames": [
                    {
                        "filename": "https://cdn.example.com/app.min.js",
                        "function": "t",
                        "lineno": 1,
                        "colno": 42,
                        "in_app": True,
                    }
                ]
            },
        },
    }
    out = sourcemaps.symbolicate_envelope(envelope, org_id, project_id, str(tmp_path))
    frame = out["exception"]["stacktrace"]["frames"][0]
    assert frame["filename"] == "../src/invoice.ts"
    assert frame["lineno"] == 4
    assert frame["raw_filename"] == "https://cdn.example.com/app.min.js"


def test_symbolicate_envelope_noop_for_non_javascript(tmp_path) -> None:
    envelope = {
        "platform": "python",
        "release": "web@1.0.0",
        "exception": {"type": "E", "value": "v", "stacktrace": {"frames": []}},
    }
    assert sourcemaps.symbolicate_envelope(envelope, "o", "p", str(tmp_path)) is envelope


def test_symbolicate_envelope_noop_without_release(tmp_path) -> None:
    envelope = {
        "platform": "javascript",
        "exception": {"type": "E", "value": "v", "stacktrace": {"frames": []}},
    }
    assert sourcemaps.symbolicate_envelope(envelope, "o", "p", str(tmp_path)) is envelope


def test_symbolicate_envelope_missing_map_is_noop(tmp_path) -> None:
    envelope = {
        "platform": "javascript",
        "release": "web@9.9.9",
        "exception": {
            "type": "Error",
            "value": "boom",
            "stacktrace": {
                "frames": [
                    {"filename": "https://x/app.min.js", "lineno": 1, "colno": 1}
                ]
            },
        },
    }
    out = sourcemaps.symbolicate_envelope(envelope, "o", "p", str(tmp_path))
    frame = out["exception"]["stacktrace"]["frames"][0]
    assert "raw_filename" not in frame  # untouched: no map on disk.


def test_load_source_map_malformed_warns_once(tmp_path) -> None:
    bad = tmp_path / "bad.map"
    bad.write_text("{ this is not json")
    sourcemaps._warned_paths.discard(str(bad))
    assert sourcemaps.load_source_map(str(bad)) is None
    assert str(bad) in sourcemaps._warned_paths


def test_parse_rejects_non_v3() -> None:
    with pytest.raises(ValueError):
        sourcemaps.parse_source_map({"version": 2, "mappings": ""})


# ==============================================================================
# Retention pruning rule (pure).
# ==============================================================================


def test_release_dirs_older_than() -> None:
    cutoff = datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC)
    old = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC).timestamp()
    fresh = datetime.datetime(2026, 7, 3, tzinfo=datetime.UTC).timestamp()
    entries = [("/maps/old", old), ("/maps/fresh", fresh)]
    assert sourcemaps.release_dirs_older_than(entries, cutoff) == ["/maps/old"]


def test_prune_expired_release_maps(tmp_path) -> None:
    org_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    project_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    old_dir = sourcemaps.release_maps_dir(str(tmp_path), org_id, project_id, "old@1")
    fresh_dir = sourcemaps.release_maps_dir(str(tmp_path), org_id, project_id, "new@2")
    os.makedirs(old_dir)
    os.makedirs(fresh_dir)
    open(os.path.join(old_dir, "a.map"), "w").close()
    open(os.path.join(fresh_dir, "a.map"), "w").close()
    # Age the old dir well past the cutoff.
    old_ts = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC).timestamp()
    os.utime(old_dir, (old_ts, old_ts))
    cutoff = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    removed = sourcemaps.prune_expired_release_maps(str(tmp_path), cutoff)
    assert removed == [old_dir]
    assert not os.path.isdir(old_dir)
    assert os.path.isdir(fresh_dir)
