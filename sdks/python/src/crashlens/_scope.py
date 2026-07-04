"""Per-context breadcrumbs, tags, and user, backed by ``contextvars``.

Using ``contextvars`` means async tasks and threads that copy the current
context each get their own view: setting a tag or adding a breadcrumb in one
task does not bleed into an unrelated task. Every mutation rebinds the variable
to a NEW container object rather than mutating in place, so a context that
copied the old reference is never affected by a later write.

Breadcrumbs are a client-side ring buffer capped at :data:`MAX_BREADCRUMBS`;
the server keeps the newest 100 as well, so we never need to send more.
"""

from __future__ import annotations

import datetime
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

MAX_BREADCRUMBS = 100

_breadcrumbs: ContextVar[List[Dict[str, Any]]] = ContextVar("crashlens_breadcrumbs")
_tags: ContextVar[Dict[str, str]] = ContextVar("crashlens_tags")
_user: ContextVar[Optional[Dict[str, Any]]] = ContextVar("crashlens_user")


def _now_rfc3339() -> str:
    dt = datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def add_breadcrumb(
    message: Optional[str] = None,
    *,
    type: Optional[str] = None,  # noqa: A002 - matches protocol field name
    category: Optional[str] = None,
    level: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """Append a breadcrumb to the current context (newest last, ring-buffered)."""
    crumb: Dict[str, Any] = {"timestamp": _now_rfc3339()}
    if type is not None:
        crumb["type"] = type
    if category is not None:
        crumb["category"] = category
    if level is not None:
        crumb["level"] = level
    if message is not None:
        crumb["message"] = message
    if data is not None:
        crumb["data"] = data

    current = _breadcrumbs.get([])
    # Copy-on-write and trim to the newest MAX_BREADCRUMBS so we never mutate a
    # list another context may still be holding.
    updated = current[-(MAX_BREADCRUMBS - 1):] + [crumb] if MAX_BREADCRUMBS > 1 else [crumb]
    _breadcrumbs.set(updated)


def set_tag(key: str, value: Any) -> None:
    """Set a single string tag on the current context."""
    current = _tags.get({})
    _tags.set({**current, str(key): str(value)})


def set_user(id: Optional[str]) -> None:  # noqa: A002 - matches protocol field name
    """Set the current user's id, or clear it when ``id`` is ``None``."""
    if id is None:
        _user.set(None)
    else:
        _user.set({"id": str(id)})


def get_breadcrumbs() -> List[Dict[str, Any]]:
    return list(_breadcrumbs.get([]))


def get_tags() -> Dict[str, str]:
    return dict(_tags.get({}))


def get_user() -> Optional[Dict[str, Any]]:
    user = _user.get(None)
    return dict(user) if user else None


def clear() -> None:
    """Reset all scope data on the current context (used mainly by tests)."""
    _breadcrumbs.set([])
    _tags.set({})
    _user.set(None)
