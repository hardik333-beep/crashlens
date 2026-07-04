"""DSN parsing.

A DSN is the single string a developer copies from their Crashlens project::

    http(s)://<public_key>@<host>[:port]/api/ingest/<project_id>/

The public key is the userinfo half of the URL. Parsing strips the userinfo and
rebuilds the endpoint URL without it, so the key never travels in the URL (it is
sent in the ``X-Crashlens-Key`` header instead).
"""

from __future__ import annotations

from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit


class Dsn(NamedTuple):
    """A parsed DSN: the ingest endpoint URL plus the public key."""

    url: str
    key: str


def parse_dsn(dsn: str) -> Dsn:
    """Parse a DSN string into its endpoint URL and public key.

    Raises ``ValueError`` for anything that is not a usable DSN.
    """
    if not isinstance(dsn, str) or not dsn.strip():
        raise ValueError("DSN must be a non-empty string")

    parts = urlsplit(dsn.strip())

    if parts.scheme not in ("http", "https"):
        raise ValueError("DSN scheme must be http or https")
    if not parts.username:
        raise ValueError("DSN is missing the public key (the part before '@')")
    if not parts.hostname:
        raise ValueError("DSN is missing a host")
    if not parts.path or parts.path == "/":
        raise ValueError("DSN is missing the ingest path")

    key = parts.username

    # Rebuild netloc WITHOUT any userinfo, preserving host and explicit port.
    host = parts.hostname
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = host if parts.port is None else f"{host}:{parts.port}"

    url = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    return Dsn(url=url, key=key)


def resolve(dsn: str = None, *, url: str = None, key: str = None) -> Dsn:
    """Resolve either a DSN string or an explicit ``url`` + ``key`` pair.

    The explicit pair is the escape hatch for setups that store the key
    separately from the endpoint. Exactly one form must be supplied.
    """
    if dsn is not None:
        if url is not None or key is not None:
            raise ValueError("Pass either dsn=... or url=...+key=..., not both")
        return parse_dsn(dsn)

    if url is None or key is None:
        raise ValueError("Provide a dsn, or both url and key")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key must be a non-empty string")

    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise ValueError("url scheme must be http or https")
    return Dsn(url=url.strip(), key=key.strip())
