"""Crasher subprocess for the Python SDK end-to-end proof.

Initialises the real Crashlens Python SDK with a DSN taken from the environment
and then raises an UNCAUGHT exception. The SDK's installed ``sys.excepthook``
(see crashlens/_hooks.py) captures the exception at level ``fatal`` and flushes
the queue synchronously before the interpreter exits, so the event reaches the
ingest endpoint even though the process terminates with a non-zero status.

Run by e2e/run_e2e.py, never directly. Reads:
  CRASHLENS_DSN - the full DSN (http://<key>@host/api/ingest/<project>/).
"""

import os

import crashlens

MARKER = "crashlens python e2e uncaught marker"


def main() -> None:
    dsn = os.environ["CRASHLENS_DSN"]
    crashlens.init(dsn, release="pysvc@1.0.0", environment="production")
    # Uncaught on purpose: the excepthook path is what we are proving.
    raise RuntimeError(MARKER)


if __name__ == "__main__":
    main()
