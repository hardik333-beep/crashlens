"""Shared test fixtures, including a real local HTTP server that captures the
requests the SDK transport makes. No network beyond localhost is used."""

from __future__ import annotations

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import pytest

from crashlens import _scope


class CapturedRequest:
    def __init__(self, path: str, headers: Dict[str, str], body: bytes) -> None:
        self.path = path
        self.headers = headers
        self.raw_body = body
        self._decoded: Optional[bytes] = None

    @property
    def is_gzip(self) -> bool:
        return self.headers.get("content-encoding", "").lower() == "gzip"

    @property
    def body(self) -> bytes:
        if self._decoded is None:
            self._decoded = (
                gzip.decompress(self.raw_body) if self.is_gzip else self.raw_body
            )
        return self._decoded

    def json(self) -> Dict[str, Any]:
        return json.loads(self.body.decode("utf-8"))


class IngestServer:
    """A localhost ingest endpoint that records requests and replies per a
    programmable response plan (status code + optional headers)."""

    def __init__(self) -> None:
        self.requests: List[CapturedRequest] = []
        self._lock = threading.Lock()
        self._event = threading.Event()
        # Each entry: (status, headers). Popped left to right; last one repeats.
        self.responses: List[tuple] = [(202, None)]
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        assert self._httpd is not None
        host, port = self._httpd.server_address
        return f"http://127.0.0.1:{port}/api/ingest/proj-1/"

    def _next_response(self) -> tuple:
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    def record(self, req: CapturedRequest) -> None:
        with self._lock:
            self.requests.append(req)
        self._event.set()

    def wait_for(self, count: int, timeout: float = 5.0) -> bool:
        import time

        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if len(self.requests) >= count:
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._event.wait(min(remaining, 0.1))
            self._event.clear()

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                pass

            def do_POST(self) -> None:  # noqa: N802 - required name
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                server.record(CapturedRequest(self.path, headers, body))

                status, extra = server._next_response()
                self.send_response(status)
                if extra:
                    for hk, hv in extra.items():
                        self.send_header(hk, hv)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id": "ok"}')

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


@pytest.fixture
def ingest_server():
    server = IngestServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(autouse=True)
def clean_scope():
    """Reset context scope around every test so breadcrumbs/tags do not bleed."""
    _scope.clear()
    yield
    _scope.clear()
