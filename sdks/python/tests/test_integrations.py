"""Integration tests. Flask / FastAPI tests skip cleanly when not installed."""

from __future__ import annotations

import importlib.util
import logging

import pytest

from crashlens._client import Client

flask_installed = importlib.util.find_spec("flask") is not None
starlette_installed = importlib.util.find_spec("starlette") is not None


# -- logging integration (stdlib only, always runs) ------------------------


def test_logging_handler_sends_error(ingest_server):
    from crashlens.logging import CrashlensHandler

    client = Client(ingest_server.url, "k")
    handler = CrashlensHandler(level=logging.ERROR, client=client)
    log = logging.getLogger("test.crashlens.errors")
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)

    log.error("something broke")
    assert ingest_server.wait_for(1)
    client.close()
    body = ingest_server.requests[0].json()
    assert body["message"] == "something broke"
    assert body["level"] == "error"


def test_logging_handler_ignores_below_level(ingest_server):
    from crashlens.logging import CrashlensHandler

    client = Client(ingest_server.url, "k")
    handler = CrashlensHandler(level=logging.ERROR, client=client)
    log = logging.getLogger("test.crashlens.belowlevel")
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)

    log.info("just info")
    log.warning("just warning")
    # Give the transport a moment; nothing should be delivered.
    assert ingest_server.wait_for(1, timeout=0.5) is False
    client.close()
    assert len(ingest_server.requests) == 0


def test_logging_handler_captures_exception(ingest_server):
    from crashlens.logging import CrashlensHandler

    client = Client(ingest_server.url, "k")
    handler = CrashlensHandler(level=logging.ERROR, client=client)
    log = logging.getLogger("test.crashlens.exc")
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)

    try:
        raise ValueError("logged exc")
    except ValueError:
        log.exception("caught it")
    assert ingest_server.wait_for(1)
    client.close()
    body = ingest_server.requests[0].json()
    assert body["exception"]["type"] == "ValueError"


def test_logging_handler_ignores_own_records(ingest_server):
    from crashlens.logging import CrashlensHandler

    client = Client(ingest_server.url, "k")
    handler = CrashlensHandler(level=logging.DEBUG, client=client)
    own = logging.getLogger("crashlens")
    own.addHandler(handler)
    try:
        own.error("internal message")
        assert ingest_server.wait_for(1, timeout=0.5) is False
        assert len(ingest_server.requests) == 0
    finally:
        own.removeHandler(handler)
        client.close()


def test_breadcrumb_handler_records_breadcrumb():
    from crashlens import _scope
    from crashlens.logging import CrashlensBreadcrumbHandler

    _scope.clear()
    handler = CrashlensBreadcrumbHandler(level=logging.INFO)
    log = logging.getLogger("test.crashlens.crumbs")
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)

    log.info("a step happened")
    crumbs = _scope.get_breadcrumbs()
    assert any(c.get("message") == "a step happened" for c in crumbs)
    _scope.clear()


# -- flask integration -----------------------------------------------------


@pytest.mark.skipif(not flask_installed, reason="flask not installed")
def test_flask_captures_request_exception(ingest_server):
    from flask import Flask

    from crashlens.flask import CrashlensFlask

    client = Client(ingest_server.url, "k")
    app = Flask(__name__)
    CrashlensFlask(app, client=client)

    @app.route("/boom")
    def boom():
        raise RuntimeError("flask boom")

    c = app.test_client()
    c.get("/boom")
    assert ingest_server.wait_for(1)
    client.close()
    body = ingest_server.requests[0].json()
    assert body["exception"]["type"] == "RuntimeError"
    assert body["request"]["method"] == "GET"
    assert "/boom" in body["request"]["url"]


@pytest.mark.skipif(flask_installed, reason="flask IS installed")
def test_flask_import_guard_raises_without_flask():
    import crashlens.flask as cf

    with pytest.raises(RuntimeError, match="requires Flask"):
        cf.init_app(object())


# -- asgi integration ------------------------------------------------------


@pytest.mark.skipif(not starlette_installed, reason="starlette/fastapi not installed")
def test_asgi_captures_and_reraises(ingest_server):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from crashlens.asgi import CrashlensMiddleware

    client = Client(ingest_server.url, "k")

    async def boom(request):
        raise RuntimeError("asgi boom")

    app = Starlette(routes=[Route("/boom", boom)])
    wrapped = CrashlensMiddleware(app, client=client)

    test_client = TestClient(wrapped, raise_server_exceptions=False)
    test_client.get("/boom")
    assert ingest_server.wait_for(1)
    client.close()
    body = ingest_server.requests[0].json()
    assert body["exception"]["type"] == "RuntimeError"
    assert body["request"]["method"] == "GET"


def test_asgi_url_builder_from_scope():
    from crashlens.asgi import _url_from_scope

    scope = {
        "type": "http",
        "scheme": "http",
        "path": "/invoices/17",
        "query_string": b"page=2",
        "headers": [(b"host", b"app.example.com")],
        "method": "POST",
    }
    url = _url_from_scope(scope)
    assert url == "http://app.example.com/invoices/17?page=2"
