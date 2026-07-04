"""DSN parsing matrix."""

from __future__ import annotations

import pytest

from crashlens import _dsn


def test_basic_https_dsn():
    d = _dsn.parse_dsn("https://pubkey123@errors.example.com/api/ingest/proj-9/")
    assert d.key == "pubkey123"
    assert d.url == "https://errors.example.com/api/ingest/proj-9/"


def test_http_scheme_with_port():
    d = _dsn.parse_dsn("http://abc@localhost:8000/api/ingest/42/")
    assert d.key == "abc"
    assert d.url == "http://localhost:8000/api/ingest/42/"


def test_key_is_stripped_from_url():
    d = _dsn.parse_dsn("https://secretkey@host/api/ingest/p/")
    assert "secretkey" not in d.url
    assert "@" not in d.url


def test_uuid_project_id_preserved():
    d = _dsn.parse_dsn(
        "https://k@h.example.com/api/ingest/3fa85f64-5717-4562-b3fc-2c963f66afa6/"
    )
    assert d.url.endswith("/api/ingest/3fa85f64-5717-4562-b3fc-2c963f66afa6/")


def test_default_ports_not_appended():
    d = _dsn.parse_dsn("https://k@host:443/api/ingest/p/")
    # An explicit port is preserved verbatim (we do not strip default ports).
    assert d.url == "https://host:443/api/ingest/p/"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "ftp://k@host/api/ingest/p/",
        "https://host/api/ingest/p/",  # no key
        "https://k@/api/ingest/p/",  # no host
        "https://k@host",  # no path
        "https://k@host/",  # empty path
        "not-a-url",
    ],
)
def test_invalid_dsns_raise(bad):
    with pytest.raises(ValueError):
        _dsn.parse_dsn(bad)


def test_resolve_with_dsn():
    d = _dsn.resolve("https://k@host/api/ingest/p/")
    assert d.key == "k"


def test_resolve_with_explicit_url_and_key():
    d = _dsn.resolve(url="https://host/api/ingest/p/", key="mykey")
    assert d.key == "mykey"
    assert d.url == "https://host/api/ingest/p/"


def test_resolve_rejects_both_forms():
    with pytest.raises(ValueError):
        _dsn.resolve("https://k@host/api/ingest/p/", url="https://x/", key="y")


def test_resolve_requires_something():
    with pytest.raises(ValueError):
        _dsn.resolve()


def test_resolve_requires_both_url_and_key():
    with pytest.raises(ValueError):
        _dsn.resolve(url="https://host/api/ingest/p/")
    with pytest.raises(ValueError):
        _dsn.resolve(key="onlykey")
