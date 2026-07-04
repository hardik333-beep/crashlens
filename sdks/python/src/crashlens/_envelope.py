"""Build protocol v1 event envelopes from Python exceptions and messages.

See ``docs/PROTOCOL.md`` for the frozen contract. Key normalisation rules this
module implements:

* frames are ordered oldest-call-first, crash frame LAST (Python's natural
  traceback order);
* the chained-exception ``cause`` field follows ``__cause__`` (explicit
  ``raise ... from ...``) preferring it over ``__context__`` (implicit chaining),
  to a maximum depth of 5;
* ``in_app`` is true when a frame's file is NOT under the standard library or an
  installed-packages directory (and, when ``in_app_module_prefixes`` is given,
  when the frame's module also matches one of those prefixes);
* source context (``context_line`` plus up to 5 lines of ``pre_context`` /
  ``post_context``) is read from ``linecache``.
"""

from __future__ import annotations

import linecache
import os
import sysconfig
import uuid
from types import FrameType, TracebackType
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import _scope

MAX_CAUSE_DEPTH = 5
MAX_CONTEXT_LINES = 5
# Keep well under the server's 128-frame cap and the 250 KB edge cap.
MAX_FRAMES = 128

SDK_NAME = "crashlens-python"

ExcInfo = Tuple[type, BaseException, Optional[TracebackType]]


def _library_paths() -> Tuple[str, ...]:
    paths = set()
    for name in ("stdlib", "platstdlib", "purelib", "platlib"):
        try:
            p = sysconfig.get_path(name)
        except (KeyError, OSError):
            p = None
        if p:
            paths.add(os.path.normcase(os.path.realpath(p)))
    return tuple(paths)


_LIBRARY_PATHS = _library_paths()


def _is_in_app(
    filename: Optional[str],
    module: Optional[str],
    prefixes: Optional[Sequence[str]],
) -> bool:
    """Return True when a frame belongs to the application, not a library."""
    if not filename:
        library = False
    else:
        try:
            fn = os.path.normcase(os.path.realpath(filename))
        except OSError:
            fn = os.path.normcase(filename)
        library = (
            "site-packages" in fn
            or "dist-packages" in fn
            or any(fn.startswith(p) for p in _LIBRARY_PATHS)
        )
    in_app = not library
    if prefixes:
        in_app = in_app and bool(module) and any(
            module.startswith(p) for p in prefixes
        )
    return in_app


def _colno(frame: FrameType, tb: TracebackType) -> int:
    """Best-effort 0-based start column of the failing instruction.

    CPython tracebacks carry no column before 3.11; ``co_positions`` supplies it
    from 3.11 onward. Returns 0 when unavailable.
    """
    code = frame.f_code
    positions = getattr(code, "co_positions", None)
    if positions is None:
        return 0
    try:
        instr_index = tb.tb_lasti // 2
        for i, pos in enumerate(code.co_positions()):
            if i == instr_index:
                start_col = pos[2]
                return start_col if isinstance(start_col, int) else 0
    except Exception:  # noqa: BLE001 - column info is strictly best-effort
        return 0
    return 0


def _source_context(
    filename: Optional[str], lineno: Optional[int]
) -> Dict[str, Any]:
    """Read the source line plus up to 5 lines of pre/post context."""
    out: Dict[str, Any] = {}
    if not filename or not lineno or lineno < 1:
        return out
    try:
        context_line = linecache.getline(filename, lineno)
    except Exception:  # noqa: BLE001 - never fail envelope build on source read
        return out
    if not context_line:
        return out
    out["context_line"] = context_line.rstrip("\n")

    pre: List[str] = []
    for n in range(max(1, lineno - MAX_CONTEXT_LINES), lineno):
        line = linecache.getline(filename, n)
        if line:
            pre.append(line.rstrip("\n"))
    if pre:
        out["pre_context"] = pre

    post: List[str] = []
    for n in range(lineno + 1, lineno + 1 + MAX_CONTEXT_LINES):
        line = linecache.getline(filename, n)
        if line:
            post.append(line.rstrip("\n"))
    if post:
        out["post_context"] = post
    return out


def _build_frames(
    tb: Optional[TracebackType], prefixes: Optional[Sequence[str]]
) -> List[Dict[str, Any]]:
    """Walk a traceback into protocol frames, oldest first and crash last."""
    frames: List[Dict[str, Any]] = []
    current = tb
    while current is not None:
        frame = current.tb_frame
        code = frame.f_code
        lineno = current.tb_lineno
        filename = code.co_filename
        module = frame.f_globals.get("__name__") if frame.f_globals else None

        entry: Dict[str, Any] = {
            "filename": filename,
            "function": code.co_name,
            "lineno": lineno if isinstance(lineno, int) else 0,
            "colno": _colno(frame, current),
            "in_app": _is_in_app(filename, module, prefixes),
        }
        entry.update(_source_context(filename, lineno))
        frames.append(entry)
        current = current.tb_next

    # A traceback is already oldest-first / crash-last. If it somehow exceeds the
    # cap, keep the frames NEAREST the crash (the last ones).
    if len(frames) > MAX_FRAMES:
        frames = frames[-MAX_FRAMES:]
    return frames


def _next_cause(exc: BaseException) -> Optional[BaseException]:
    """Explicit ``__cause__`` wins; fall back to implicit ``__context__``."""
    if exc.__cause__ is not None:
        return exc.__cause__
    if not getattr(exc, "__suppress_context__", False) and exc.__context__ is not None:
        return exc.__context__
    return None


def _build_exception(
    exc: BaseException,
    prefixes: Optional[Sequence[str]],
    depth: int,
) -> Dict[str, Any]:
    """Build one exception object, recursing into the cause chain up to depth 5."""
    obj: Dict[str, Any] = {
        "type": type(exc).__name__,
        "value": _safe_str(exc),
        "stacktrace": {"frames": _build_frames(exc.__traceback__, prefixes)},
    }
    if depth < MAX_CAUSE_DEPTH:
        cause = _next_cause(exc)
        if cause is not None and cause is not exc:
            obj["cause"] = _build_exception(cause, prefixes, depth + 1)
    return obj


def _safe_str(value: Any) -> str:
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - a broken __str__ must not break capture
        return f"<unprintable {type(value).__name__}>"


def _now_rfc3339() -> str:
    return _scope._now_rfc3339()


def build_event(
    *,
    sdk_version: str,
    environment: str,
    release: Optional[str],
    prefixes: Optional[Sequence[str]],
    level: str,
    message: Optional[str] = None,
    exc_info: Optional[ExcInfo] = None,
    request: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble a full protocol envelope. At least one of message/exc_info set."""
    event: Dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "timestamp": _now_rfc3339(),
        "platform": "python",
        "level": level,
        "environment": environment,
        "sdk": {"name": SDK_NAME, "version": sdk_version},
    }
    if message is not None:
        event["message"] = message
    if exc_info is not None and exc_info[1] is not None:
        event["exception"] = _build_exception(exc_info[1], prefixes, depth=1)
    if release:
        event["release"] = release

    breadcrumbs = _scope.get_breadcrumbs()
    if breadcrumbs:
        event["breadcrumbs"] = breadcrumbs
    tags = _scope.get_tags()
    if tags:
        event["tags"] = tags
    user = _scope.get_user()
    if user:
        event["user"] = user
    if request:
        event["request"] = {k: v for k, v in request.items() if v is not None}

    return event
