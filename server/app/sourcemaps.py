"""JavaScript source map storage layout, parsing, and worker-side symbolication.

This module is the data-and-CPU half of W6-01. It has THREE concerns, all pure
or filesystem-only (no database, no network):

1. STORAGE LAYOUT (path safety). Uploaded ``.map`` files live on disk at::

       {SOURCEMAPS_DIR}/{org_id}/{project_id}/{release_dir}/{basename}

   ``org_id`` and ``project_id`` are server-verified uuids (the route proves the
   caller is an admin of ``org_id`` and that ``project_id`` belongs to it before
   any path is built). ``release_dir`` is derived from the client release string
   by :func:`release_dirname` -- UNPADDED URL-SAFE BASE64 of the UTF-8 release
   string. That choice is deliberate:

     * REVERSIBLE -- the GET listing recovers the exact original release string
       (:func:`release_from_dirname`) without a side metadata file;
     * PATH-SAFE -- the base64url alphabet is ``[A-Za-z0-9_-]`` only, so a
       release dir name can never contain ``/``, ``\\``, ``.``, or ``..`` and a
       traversal is structurally impossible regardless of the release string;
     * COLLISION-FREE -- distinct release strings encode to distinct dir names
       (unlike a lossy slugify, where ``web@1.4.2`` and ``web#1.4.2`` would
       collide).

   ``basename`` is always reduced with :func:`safe_basename` (``os.path.basename``
   plus a reject on any residual separator or ``..``); the client's field name and
   any directory component it sends are discarded. Client paths are NEVER trusted.

2. SOURCE MAP PARSING. :func:`decode_vlq` and :func:`parse_source_map` implement
   just enough of the Source Map v3 spec (base64 VLQ ``mappings``) in pure Python
   to look up an original position -- no new runtime dependency. Parsed maps are
   memoised by (path, mtime) in a bounded cache (:func:`load_source_map`).

3. SYMBOLICATION. :func:`symbolicate_envelope` rewrites the frames of a
   ``javascript``-platform exception event IN PLACE-ish (returns a modified deep
   structure; callers pass the already-normalized envelope) using whatever maps
   are on disk for the event's release. It is defensive to a fault: a missing map,
   a malformed map, or an out-of-range position leaves the frame untouched and
   never raises, so symbolication can never fail an event.

SECRETS / PII HYGIENE: this module logs ONLY ids, counts, file paths, and byte
sizes. It NEVER logs source map contents, ``sourcesContent`` source text, frame
contents, or any event payload.
"""

import base64
import binascii
import datetime
import functools
import json
import logging
import os
import shutil

logger = logging.getLogger(__name__)

# Source Map v3 base64 VLQ alphabet (RFC 4648 standard base64, NOT url-safe: the
# mappings field uses '+' and '/').
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B64_INDEX = {ch: i for i, ch in enumerate(_B64)}

_VLQ_CONTINUATION = 0x20  # high bit of each 6-bit group: more groups follow.
_VLQ_MASK = 0x1F  # low 5 data bits of each group.

# Context lines captured around the symbolicated line. The protocol caps
# pre/post_context at 5 lines each (docs/PROTOCOL.md section 3.3); we honor that.
CONTEXT_WINDOW = 5

# Bounded in-process cache of parsed maps, keyed by (abspath, mtime_ns). A new
# mtime (a re-upload) is a distinct key, so a stale parse is never served.
# FLAGGED DEFAULT (governor review): maxsize 32.
_MAP_CACHE_MAXSIZE = 32

# Malformed-map warnings are emitted once per path to avoid log spam when the
# same bad map is hit by every frame of every event in a release.
_warned_paths: set[str] = set()


# ==============================================================================
# Path safety (pure).
# ==============================================================================


def release_dirname(release: str) -> str:
    """Return the on-disk directory name for ``release`` (unpadded base64url).

    Reversible and path-safe by construction (see module docstring). ``release``
    is the raw client string; the result contains only ``[A-Za-z0-9_-]``.
    """
    encoded = base64.urlsafe_b64encode(release.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def release_from_dirname(dirname: str) -> str | None:
    """Return the original release string for a base64url ``dirname``, or None.

    Re-pads before decoding. Returns None for any name that is not valid
    unpadded base64url of UTF-8 (defensive: a stray directory a human dropped in
    the tree must not crash the GET listing).
    """
    try:
        padded = dirname + "=" * (-len(dirname) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def safe_basename(name: str) -> str | None:
    """Return a trusted basename for an uploaded file, or None if unsafe.

    Reduces ``name`` with ``os.path.basename`` and then REJECTS (returns None)
    anything still carrying a path separator, a ``..`` component, or that is
    empty / ``.`` / ``..``. The client's original path is never trusted; only a
    clean single filename survives.
    """
    if not isinstance(name, str) or not name:
        return None
    base = os.path.basename(name)
    if base in ("", ".", ".."):
        return None
    if "/" in base or "\\" in base or ".." in base or "\x00" in base:
        return None
    return base


def basename_from_frame_filename(filename: object) -> str | None:
    """Return the asset basename from a frame ``filename`` URL, or None.

    Browser frames carry full URLs (e.g. ``https://cdn.example/app.min.js`` or
    ``https://cdn.example/app.min.js?v=3``). We take the last path segment and
    strip a query/fragment, then reduce it with :func:`safe_basename`.
    """
    if not isinstance(filename, str) or not filename:
        return None
    # Drop scheme//host and query/fragment: everything after the last '/'.
    tail = filename.split("?", 1)[0].split("#", 1)[0]
    tail = tail.rsplit("/", 1)[-1]
    return safe_basename(tail)


def map_candidates(basename: str) -> list[str]:
    """Return candidate ``.map`` filenames for an asset ``basename``.

    An asset ``app.min.js`` is mapped by ``app.min.js.map``; if the frame already
    named a ``.map`` file, use it as-is first.
    """
    if basename.endswith(".map"):
        return [basename]
    return [basename + ".map"]


# ==============================================================================
# Storage layout (filesystem).
# ==============================================================================


def project_maps_dir(sourcemaps_dir: str, org_id: str, project_id: str) -> str:
    """Return the absolute directory holding all releases for one project."""
    return os.path.join(sourcemaps_dir, str(org_id), str(project_id))


def release_maps_dir(
    sourcemaps_dir: str, org_id: str, project_id: str, release: str
) -> str:
    """Return the absolute directory for one release's maps (base64url dir name)."""
    return os.path.join(
        project_maps_dir(sourcemaps_dir, org_id, project_id), release_dirname(release)
    )


def destination_path(
    sourcemaps_dir: str, org_id: str, project_id: str, release: str, basename: str
) -> str:
    """Return the absolute path a ``.map`` file is stored at.

    ``basename`` MUST already have passed :func:`safe_basename`; this only joins.
    """
    return os.path.join(
        release_maps_dir(sourcemaps_dir, org_id, project_id, release), basename
    )


def list_release_maps(
    sourcemaps_dir: str, org_id: str, project_id: str
) -> list[dict]:
    """Return ``[{release, files:[{basename, size, uploaded_at}]}]`` for a project.

    Reads the on-disk tree and decodes each release directory name back to its
    original release string. Releases are sorted by newest file mtime first;
    files within a release by basename. Directories whose name is not valid
    base64url (a stray dir) are skipped defensively. Only ``.map`` files are
    listed (that is all that is ever stored).
    """
    base = project_maps_dir(sourcemaps_dir, org_id, project_id)
    try:
        release_dirs = os.listdir(base)
    except OSError:
        return []
    releases: list[tuple[float, dict]] = []
    for dirname in release_dirs:
        release = release_from_dirname(dirname)
        if release is None:
            continue
        release_path = os.path.join(base, dirname)
        if not os.path.isdir(release_path):
            continue
        files = []
        newest = 0.0
        try:
            entries = os.listdir(release_path)
        except OSError:
            continue
        for name in entries:
            if not name.endswith(".map"):
                continue
            file_path = os.path.join(release_path, name)
            try:
                stat = os.stat(file_path)
            except OSError:
                continue
            newest = max(newest, stat.st_mtime)
            files.append(
                {
                    "basename": name,
                    "size": stat.st_size,
                    "uploaded_at": datetime.datetime.fromtimestamp(
                        stat.st_mtime, datetime.UTC
                    ).isoformat(),
                }
            )
        if not files:
            continue
        files.sort(key=lambda f: f["basename"])
        releases.append((newest, {"release": release, "files": files}))
    releases.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _newest, entry in releases]


def delete_release_maps(
    sourcemaps_dir: str, org_id: str, project_id: str, release: str
) -> bool:
    """Remove a release's entire map directory. Return True if one was removed.

    The release dir name is derived by :func:`release_dirname` (base64url), so the
    target is always inside the project tree regardless of the release string --
    no traversal is possible.
    """
    release_path = release_maps_dir(sourcemaps_dir, org_id, project_id, release)
    if not os.path.isdir(release_path):
        return False
    shutil.rmtree(release_path)
    return True


# ==============================================================================
# Retention (filesystem cleanup).
# ==============================================================================


def release_dirs_older_than(
    entries: list[tuple[str, float]], cutoff: datetime.datetime
) -> list[str]:
    """Return the paths in ``entries`` whose mtime is strictly before ``cutoff``.

    Pure function (no I/O): ``entries`` is ``[(path, mtime_epoch_seconds), ...]``
    and ``cutoff`` is a timezone-aware datetime. Split out so the retention rule
    is unit-testable without touching the filesystem.
    """
    cutoff_ts = cutoff.timestamp()
    return [path for path, mtime in entries if mtime < cutoff_ts]


def _walk_release_dirs(sourcemaps_dir: str) -> list[tuple[str, float]]:
    """Return ``[(release_dir_abspath, mtime), ...]`` across every org/project.

    Layout is ``{sourcemaps_dir}/{org}/{project}/{release}``; anything that does
    not match that depth is ignored (defensive). Missing root -> empty list.
    """
    found: list[tuple[str, float]] = []
    try:
        orgs = os.listdir(sourcemaps_dir)
    except OSError:
        return found
    for org in orgs:
        org_path = os.path.join(sourcemaps_dir, org)
        if not os.path.isdir(org_path):
            continue
        try:
            projects_ = os.listdir(org_path)
        except OSError:
            continue
        for project in projects_:
            project_path = os.path.join(org_path, project)
            if not os.path.isdir(project_path):
                continue
            try:
                releases = os.listdir(project_path)
            except OSError:
                continue
            for release in releases:
                release_path = os.path.join(project_path, release)
                if not os.path.isdir(release_path):
                    continue
                try:
                    mtime = os.stat(release_path).st_mtime
                except OSError:
                    continue
                found.append((release_path, mtime))
    return found


def prune_expired_release_maps(
    sourcemaps_dir: str, cutoff: datetime.datetime
) -> list[str]:
    """Remove release map directories older than ``cutoff``. Return removed paths.

    Uses directory mtime as the age signal (mtime advances when a file is
    written into the directory, i.e. on upload). Never raises on a per-directory
    error: a directory that vanishes or cannot be removed is skipped and logged.
    """
    expired = release_dirs_older_than(_walk_release_dirs(sourcemaps_dir), cutoff)
    removed: list[str] = []
    for path in expired:
        try:
            shutil.rmtree(path)
            removed.append(path)
        except OSError:
            logger.warning("sourcemaps: could not remove expired release dir path=%s", path)
    return removed


# ==============================================================================
# Source Map v3 parsing (pure).
# ==============================================================================


def decode_vlq(segment: str) -> list[int]:
    """Decode one base64 VLQ ``segment`` into its list of signed integers.

    Each value is a run of base64 digits; the high bit (0x20) marks continuation
    and the least-significant bit of the first group is the sign. Raises
    ``ValueError`` on a non-base64 character or a truncated (dangling
    continuation) value, so the caller can treat the whole map as malformed.
    """
    values: list[int] = []
    shift = 0
    acc = 0
    have_bits = False
    for ch in segment:
        digit = _B64_INDEX.get(ch)
        if digit is None:
            raise ValueError(f"invalid base64 VLQ character: {ch!r}")
        have_bits = True
        acc += (digit & _VLQ_MASK) << shift
        if digit & _VLQ_CONTINUATION:
            shift += 5
        else:
            sign_negative = acc & 1
            magnitude = acc >> 1
            values.append(-magnitude if sign_negative else magnitude)
            acc = 0
            shift = 0
            have_bits = False
    if have_bits:
        raise ValueError("truncated VLQ value (dangling continuation bit)")
    return values


class SourceMap:
    """A parsed Source Map v3, indexed for original-position lookup.

    ``lines`` maps a 0-based generated line number to a list of segments sorted by
    generated column. Each segment is a tuple
    ``(gen_col, src_index, orig_line, orig_col, name_index_or_None)``; segments
    with only a generated column (no source binding) are dropped, since they
    cannot yield an original position.
    """

    __slots__ = ("sources", "sources_content", "names", "lines")

    def __init__(
        self,
        sources: list[str],
        sources_content: list[str | None],
        names: list[str],
        lines: dict[int, list[tuple[int, int, int, int, int | None]]],
    ) -> None:
        self.sources = sources
        self.sources_content = sources_content
        self.names = names
        self.lines = lines

    def lookup(
        self, gen_line: int, gen_col: int
    ) -> tuple[int, int, int, int | None] | None:
        """Return ``(src_index, orig_line, orig_col, name_index)`` for a generated position.

        Picks the segment on ``gen_line`` with the greatest generated column that
        is ``<= gen_col`` (the mapping that started at or before the queried
        column). ``gen_line`` / ``gen_col`` are 0-based. Returns None when the line
        has no mappings or all of them start after ``gen_col``.
        """
        segments = self.lines.get(gen_line)
        if not segments:
            return None
        chosen = None
        for seg in segments:
            if seg[0] <= gen_col:
                chosen = seg
            else:
                break
        if chosen is None:
            return None
        return (chosen[1], chosen[2], chosen[3], chosen[4])

    def source_context(
        self, src_index: int, orig_line: int
    ) -> tuple[str | None, list[str], list[str]]:
        """Return ``(context_line, pre_context, post_context)`` from ``sourcesContent``.

        ``orig_line`` is 0-based. Returns ``(None, [], [])`` when the source has no
        embedded content or the line is out of range. pre/post are capped to
        ``CONTEXT_WINDOW`` lines.
        """
        if not 0 <= src_index < len(self.sources_content):
            return (None, [], [])
        content = self.sources_content[src_index]
        if not isinstance(content, str):
            return (None, [], [])
        src_lines = content.split("\n")
        if not 0 <= orig_line < len(src_lines):
            return (None, [], [])
        context_line = src_lines[orig_line]
        pre = src_lines[max(0, orig_line - CONTEXT_WINDOW):orig_line]
        post = src_lines[orig_line + 1:orig_line + 1 + CONTEXT_WINDOW]
        return (context_line, pre, post)


def parse_source_map(raw: dict) -> SourceMap:
    """Parse a decoded Source Map v3 ``raw`` dict into a :class:`SourceMap`.

    Raises ``ValueError`` if the map is not a recognisable v3 map or a VLQ
    segment is malformed; the caller catches this and skips symbolication.
    """
    if not isinstance(raw, dict) or raw.get("version") != 3:
        raise ValueError("not a Source Map v3 document")
    mappings = raw.get("mappings")
    if not isinstance(mappings, str):
        raise ValueError("mappings is missing or not a string")
    sources = [s if isinstance(s, str) else "" for s in raw.get("sources") or []]
    contents_raw = raw.get("sourcesContent") or []
    sources_content: list[str | None] = [
        c if isinstance(c, str) else None for c in contents_raw
    ]
    names = [n if isinstance(n, str) else "" for n in raw.get("names") or []]

    lines: dict[int, list[tuple[int, int, int, int, int | None]]] = {}
    # Fields 2-5 are cumulative across the WHOLE file; only the generated column
    # (field 1) resets at each generated line.
    src_index = 0
    orig_line = 0
    orig_col = 0
    name_index = 0
    for gen_line, line in enumerate(mappings.split(";")):
        if not line:
            continue
        gen_col = 0
        row: list[tuple[int, int, int, int, int | None]] = []
        for raw_segment in line.split(","):
            if not raw_segment:
                continue
            fields = decode_vlq(raw_segment)
            if not fields:
                continue
            gen_col += fields[0]
            if len(fields) >= 4:
                src_index += fields[1]
                orig_line += fields[2]
                orig_col += fields[3]
                seg_name: int | None = None
                if len(fields) >= 5:
                    name_index += fields[4]
                    seg_name = name_index
                row.append((gen_col, src_index, orig_line, orig_col, seg_name))
            # A 1-field segment (generated column only) has no source binding;
            # skip it -- it can never produce an original position.
        if row:
            row.sort(key=lambda seg: seg[0])
            lines[gen_line] = row
    return SourceMap(sources, sources_content, names, lines)


@functools.lru_cache(maxsize=_MAP_CACHE_MAXSIZE)
def _parse_cached(path: str, mtime_ns: int) -> SourceMap | None:
    """Read+parse the map at ``path`` (cache keyed by path+mtime). None if unusable.

    ``mtime_ns`` participates only as a cache key so a re-uploaded map (new mtime)
    misses the cache and is re-parsed. A malformed or unreadable map warns once
    per path and yields None so symbolication is skipped, never failed.
    """
    try:
        with open(path, "rb") as handle:
            raw = json.loads(handle.read().decode("utf-8"))
        return parse_source_map(raw)
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        if path not in _warned_paths:
            _warned_paths.add(path)
            logger.warning("sourcemaps: skipping malformed or unreadable map path=%s", path)
        return None


def load_source_map(path: str) -> SourceMap | None:
    """Return the parsed :class:`SourceMap` at ``path``, or None if absent/malformed.

    Stats the file to key the parse cache by modification time. A missing file is
    simply None (not an error): the release has no map for that asset.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return _parse_cached(path, stat.st_mtime_ns)


# ==============================================================================
# Symbolication (filesystem-backed, defensive).
# ==============================================================================


def _symbolicate_frame(frame: dict, source_map: SourceMap) -> dict:
    """Return a symbolicated copy of ``frame`` using ``source_map``, or the input.

    Preserves the minified values as ``raw_filename`` / ``raw_lineno`` and sets
    ``filename`` / ``lineno`` / ``colno`` / ``function`` to the originals. Sets
    ``in_app`` false for original paths under ``node_modules``. Any missing or
    out-of-range position leaves the frame unchanged.
    """
    lineno = frame.get("lineno")
    colno = frame.get("colno")
    if not isinstance(lineno, int) or not isinstance(colno, int):
        return frame
    # Frames are 1-based (browser convention); the map is 0-based. A colno of 0
    # (older SDK / unknown column) maps to generated column 0.
    gen_line = lineno - 1
    gen_col = max(colno - 1, 0)
    resolved = source_map.lookup(gen_line, gen_col)
    if resolved is None:
        return frame
    src_index, orig_line, orig_col, name_index = resolved
    if not 0 <= src_index < len(source_map.sources):
        return frame

    original_path = source_map.sources[src_index] or frame.get("filename")
    out = dict(frame)
    out["raw_filename"] = frame.get("filename")
    out["raw_lineno"] = frame.get("lineno")
    out["filename"] = original_path
    out["lineno"] = orig_line + 1
    out["colno"] = orig_col + 1
    if name_index is not None and 0 <= name_index < len(source_map.names):
        name = source_map.names[name_index]
        if name:
            out["function"] = name
    if isinstance(original_path, str) and "node_modules" in original_path:
        out["in_app"] = False

    context_line, pre_context, post_context = source_map.source_context(
        src_index, orig_line
    )
    if context_line is not None:
        out["context_line"] = context_line
        out["pre_context"] = pre_context
        out["post_context"] = post_context
    return out


def _symbolicate_exception(
    exception: dict, sourcemaps_dir: str, org_id: str, project_id: str, release: str
) -> dict:
    """Symbolicate every frame of ``exception`` and, recursively, its ``cause``."""
    out = dict(exception)
    stacktrace = out.get("stacktrace")
    if isinstance(stacktrace, dict):
        frames = stacktrace.get("frames")
        if isinstance(frames, list):
            release_dir = release_maps_dir(sourcemaps_dir, org_id, project_id, release)
            new_frames = []
            for frame in frames:
                if not isinstance(frame, dict):
                    new_frames.append(frame)
                    continue
                basename = basename_from_frame_filename(frame.get("filename"))
                source_map = None
                if basename is not None:
                    for candidate in map_candidates(basename):
                        source_map = load_source_map(os.path.join(release_dir, candidate))
                        if source_map is not None:
                            break
                if source_map is None:
                    new_frames.append(frame)
                    continue
                new_frames.append(_symbolicate_frame(frame, source_map))
            out["stacktrace"] = {**stacktrace, "frames": new_frames}
    cause = out.get("cause")
    if isinstance(cause, dict):
        out["cause"] = _symbolicate_exception(
            cause, sourcemaps_dir, org_id, project_id, release
        )
    return out


def symbolicate_envelope(
    envelope: dict, org_id: str, project_id: str, sourcemaps_dir: str
) -> dict:
    """Return ``envelope`` with JavaScript frames symbolicated where maps exist.

    A no-op (returns the input unchanged) unless the event is
    ``platform == "javascript"``, carries an ``exception``, and has a non-empty
    ``release`` to key the map directory. Fully defensive: any error inside is
    swallowed and the original envelope returned, so symbolication can never fail
    an event (the worker's poison guard stays the last resort, not this).

    NOTE (documented v1 behaviour): symbolication runs BEFORE fingerprinting, so a
    symbolicated event fingerprints on its ORIGINAL frames. An event that arrives
    before its map is uploaded fingerprints on minified frames and therefore lands
    in a DIFFERENT Issue than the same crash symbolicated. This is accepted at v1.
    """
    try:
        if not isinstance(envelope, dict):
            return envelope
        if envelope.get("platform") != "javascript":
            return envelope
        exception = envelope.get("exception")
        release = envelope.get("release")
        if not isinstance(exception, dict) or not isinstance(release, str) or not release:
            return envelope
        out = dict(envelope)
        out["exception"] = _symbolicate_exception(
            exception, sourcemaps_dir, str(org_id), str(project_id), release
        )
        return out
    except Exception:  # noqa: BLE001 - symbolication must never fail an event
        logger.warning(
            "sourcemaps: symbolication error (skipped) org_id=%s project_id=%s",
            org_id,
            project_id,
        )
        return envelope
