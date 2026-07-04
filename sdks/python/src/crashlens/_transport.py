"""Background, non-blocking HTTP transport.

A single daemon thread drains a bounded queue and POSTs one event per request.
Design rules from the brief:

* the app thread NEVER blocks: :meth:`Transport.submit` drops the newest event
  when the queue is full (warning once) rather than waiting;
* the body is gzip-compressed when it exceeds :data:`GZIP_THRESHOLD`;
* on a connection error or 5xx we retry ONCE after a short delay, then drop;
* on 429 we sleep up to :data:`MAX_RETRY_AFTER` seconds honouring ``Retry-After``
  and drop (never re-queue);
* 4xx (other than 429) drops immediately;
* nothing here ever raises into the host application.
"""

from __future__ import annotations

import gzip
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
import warnings
from typing import Any, Dict, Optional

logger = logging.getLogger("crashlens")

GZIP_THRESHOLD = 4096  # 4 KiB
RETRY_DELAY = 0.5  # seconds, single retry on connection error / 5xx
MAX_RETRY_AFTER = 5  # seconds, cap on honouring a 429 Retry-After

_SHUTDOWN = object()


class Transport:
    def __init__(
        self,
        url: str,
        key: str,
        *,
        timeout: float = 2.0,
        max_queue: int = 100,
    ) -> None:
        self._url = url
        self._key = key
        self._timeout = timeout
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=max_queue)
        self._cond = threading.Condition()
        self._pending = 0
        self._closed = False
        self._warned_full = False
        self._thread = threading.Thread(
            target=self._run, name="crashlens-transport", daemon=True
        )
        self._thread.start()

    # -- producer side -----------------------------------------------------

    def submit(self, envelope: Dict[str, Any]) -> None:
        """Enqueue an event without blocking. Drops (newest) when full."""
        if self._closed:
            return
        with self._cond:
            try:
                self._queue.put_nowait(envelope)
            except queue.Full:
                if not self._warned_full:
                    warnings.warn(
                        "crashlens: event queue is full; dropping events",
                        stacklevel=2,
                    )
                    self._warned_full = True
                return
            self._pending += 1

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until the queue drains or ``timeout`` elapses. Returns success."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._pending > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return True

    def close(self, timeout: float = 5.0) -> None:
        """Flush, stop the worker, and join it. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self.flush(timeout)
        try:
            self._queue.put_nowait(_SHUTDOWN)
        except queue.Full:
            # Make room by draining one item, then signal shutdown.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(_SHUTDOWN)
            except queue.Full:
                pass
        self._thread.join(timeout=timeout)

    # -- consumer side -----------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SHUTDOWN:
                return
            try:
                self._send(item)
            except Exception:  # noqa: BLE001 - transport must never crash
                logger.debug("crashlens: event send failed", exc_info=True)
            finally:
                with self._cond:
                    self._pending -= 1
                    if self._pending <= 0:
                        self._cond.notify_all()

    def _serialize(self, envelope: Dict[str, Any]) -> "tuple[bytes, dict[str, str]]":
        body = json.dumps(
            envelope, default=str, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Crashlens-Key": self._key,
            "User-Agent": "crashlens-python",
        }
        if len(body) > GZIP_THRESHOLD:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        return body, headers

    def _send(self, envelope: Dict[str, Any]) -> None:
        body, headers = self._serialize(envelope)
        for attempt in (0, 1):
            try:
                status, retry_after = self._post(body, headers)
            except (urllib.error.URLError, TimeoutError, OSError):
                # Connection-level failure: retry once, then drop.
                if attempt == 0:
                    time.sleep(RETRY_DELAY)
                    continue
                logger.debug("crashlens: connection error, dropping event")
                return
            if status == 429:
                wait = min(retry_after if retry_after is not None else 0, MAX_RETRY_AFTER)
                if wait > 0:
                    time.sleep(wait)
                logger.debug("crashlens: rate limited (429), dropping event")
                return
            if 500 <= status < 600:
                if attempt == 0:
                    time.sleep(RETRY_DELAY)
                    continue
                logger.debug("crashlens: server error %s, dropping event", status)
                return
            # 2xx success or a 4xx we cannot fix: done either way.
            return

    def _post(
        self, body: bytes, headers: Dict[str, str]
    ) -> "tuple[int, Optional[int]]":
        req = urllib.request.Request(
            self._url, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                resp.read()
                return resp.status, None
        except urllib.error.HTTPError as exc:
            retry_after = _parse_retry_after(exc.headers.get("Retry-After"))
            try:
                exc.read()
            except Exception:  # noqa: BLE001 - draining is best-effort
                pass
            return exc.code, retry_after


def _parse_retry_after(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return max(0, int(float(value.strip())))
    except (ValueError, AttributeError):
        return None
