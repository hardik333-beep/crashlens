"""Transport tests against a real localhost HTTP server."""

from __future__ import annotations

import threading
import time

import pytest

from crashlens import _hooks
from crashlens._client import Client
from crashlens._transport import GZIP_THRESHOLD, Transport


def _event(message="hello", **extra):
    ev = {
        "event_id": "id-" + str(extra.get("n", 0)),
        "timestamp": "2026-07-04T12:00:00.000Z",
        "platform": "python",
        "level": "error",
        "environment": "test",
        "sdk": {"name": "crashlens-python", "version": "0.1.0"},
        "message": message,
    }
    ev.update(extra)
    return ev


def test_single_event_posts_once_with_key_header(ingest_server):
    t = Transport(ingest_server.url, "mykey", timeout=2.0, max_queue=10)
    t.submit(_event())
    assert ingest_server.wait_for(1)
    t.close()

    assert len(ingest_server.requests) == 1
    req = ingest_server.requests[0]
    assert req.path == "/api/ingest/proj-1/"
    assert req.headers.get("x-crashlens-key") == "mykey"
    assert req.json()["message"] == "hello"


def test_one_event_per_post(ingest_server):
    t = Transport(ingest_server.url, "k", max_queue=50)
    for i in range(5):
        t.submit(_event(n=i))
    assert ingest_server.wait_for(5)
    t.close()
    assert len(ingest_server.requests) == 5
    ids = sorted(r.json()["event_id"] for r in ingest_server.requests)
    assert ids == [f"id-{i}" for i in range(5)]


def test_small_body_not_gzipped(ingest_server):
    t = Transport(ingest_server.url, "k")
    t.submit(_event())
    assert ingest_server.wait_for(1)
    t.close()
    assert ingest_server.requests[0].is_gzip is False


def test_large_body_is_gzipped_and_decodes(ingest_server):
    t = Transport(ingest_server.url, "k")
    big = "x" * (GZIP_THRESHOLD * 2)
    t.submit(_event(message=big))
    assert ingest_server.wait_for(1)
    t.close()
    req = ingest_server.requests[0]
    assert req.is_gzip is True
    assert req.json()["message"] == big  # gzip round-trips


def test_flush_drains_queue(ingest_server):
    t = Transport(ingest_server.url, "k", max_queue=100)
    for i in range(10):
        t.submit(_event(n=i))
    assert t.flush(timeout=5.0) is True
    assert len(ingest_server.requests) == 10
    t.close()


def test_queue_overflow_drops_without_blocking(ingest_server):
    # A slow server plus a tiny queue: submit must never block the caller.
    ingest_server.responses = [(202, None)]
    t = Transport(ingest_server.url, "k", max_queue=2)
    start = time.monotonic()
    with pytest.warns(UserWarning, match="queue is full"):
        for i in range(500):
            t.submit(_event(n=i))
    elapsed = time.monotonic() - start
    # 500 submits into a 2-slot queue must return fast (no blocking on network).
    assert elapsed < 2.0
    t.close(timeout=5.0)
    # Far fewer than 500 were delivered; the rest were dropped, not queued.
    assert len(ingest_server.requests) < 500


def test_429_is_dropped_not_requeued(ingest_server):
    ingest_server.responses = [(429, {"Retry-After": "1"}), (202, None)]
    t = Transport(ingest_server.url, "k")
    t.submit(_event())
    assert ingest_server.wait_for(1)
    t.close()
    # Exactly one POST: the 429 caused a drop, not a re-queue/retry.
    assert len(ingest_server.requests) == 1


def test_500_retries_once(ingest_server):
    ingest_server.responses = [(500, None), (202, None)]
    t = Transport(ingest_server.url, "k")
    t.submit(_event())
    assert ingest_server.wait_for(2)
    t.close()
    assert len(ingest_server.requests) == 2  # original + one retry


def test_client_capture_message_end_to_end(ingest_server):
    client = Client(ingest_server.url, "k", environment="test")
    eid = client.capture_message("via client", level="warning")
    assert eid is not None
    assert ingest_server.wait_for(1)
    client.flush(2.0)
    client.close()
    body = ingest_server.requests[0].json()
    assert body["message"] == "via client"
    assert body["level"] == "warning"
    assert body["environment"] == "test"


def test_client_capture_exception_end_to_end(ingest_server):
    client = Client(ingest_server.url, "k", environment="test")
    try:
        raise KeyError("missing")
    except KeyError as e:
        client.capture_exception(e)
    assert ingest_server.wait_for(1)
    client.close()
    body = ingest_server.requests[0].json()
    assert body["exception"]["type"] == "KeyError"


def test_capture_never_raises_on_bad_endpoint():
    # Nothing listening on this port: capture + flush must stay silent.
    client = Client("http://127.0.0.1:1/api/ingest/p/", "k", timeout=0.5)
    eid = client.capture_message("into the void")
    assert eid is not None
    client.close(timeout=2.0)


@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)
def test_threading_excepthook_fires(ingest_server):
    client = Client(ingest_server.url, "k")
    _hooks._reset_for_tests()
    _hooks.install(client)
    try:
        def boom():
            raise RuntimeError("thread crash")

        th = threading.Thread(target=boom)
        th.start()
        th.join()
        assert ingest_server.wait_for(1)
        body = ingest_server.requests[0].json()
        assert body["exception"]["type"] == "RuntimeError"
        assert body["level"] == "fatal"
    finally:
        _hooks._reset_for_tests()
        client.close()
