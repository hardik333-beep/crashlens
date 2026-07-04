# Crashlens Python SDK

Report errors from any Python application to your self-hosted Crashlens
instance. One line to start: add an init call with your project key and
unhandled exceptions start showing up in your dashboard.

- Zero runtime dependencies (standard library only).
- Non-blocking: events are sent from a background thread and never slow down or
  crash your app.
- Works with plain Python, Flask, FastAPI, Starlette, and the logging module.

## Install

While this SDK is pre-release, install it from the source tree:

```bash
pip install -e sdks/python
```

## Quick start

Copy the connection string from your Crashlens project and pass it to `init`:

```python
import crashlens

crashlens.init("https://YOUR_PUBLIC_KEY@your-crashlens-host/api/ingest/YOUR_PROJECT_ID/")

# From here on, any unhandled exception is reported automatically.
raise RuntimeError("this shows up in your dashboard")
```

If you prefer to keep the key separate from the URL, use the explicit form:

```python
crashlens.init(
    url="https://your-crashlens-host/api/ingest/YOUR_PROJECT_ID/",
    key="YOUR_PUBLIC_KEY",
)
```

### Options

```python
crashlens.init(
    "https://KEY@host/api/ingest/PROJECT/",
    environment="production",         # shown on every event
    release="web@1.4.2",              # release identifier
    in_app_module_prefixes=["myapp"], # mark these modules as your code
    max_queue=100,                    # in-memory buffer size
    timeout=2.0,                      # per-request network timeout (seconds)
    install_excepthooks=True,         # capture unhandled exceptions
)
```

## Capturing errors and context

```python
import crashlens

# Handled exception
try:
    risky()
except Exception:
    crashlens.capture_exception()      # uses the exception being handled

# Or pass one explicitly
crashlens.capture_exception(some_exception)

# A log-style message with no exception
crashlens.capture_message("payment gateway slow", level="warning")

# Context that travels with the next event
crashlens.set_user("user-123")
crashlens.set_tag("region", "eu-west")
crashlens.add_breadcrumb("loaded invoice 17", category="db")

# Make sure queued events are sent before the process exits
crashlens.flush(timeout=5.0)
```

Breadcrumbs, tags, and user are stored per context (using `contextvars`), so
async tasks and threads do not leak each other's data. Breadcrumbs keep the
newest 100.

## Flask

```python
from flask import Flask
from crashlens.flask import CrashlensFlask
import crashlens

crashlens.init("https://KEY@host/api/ingest/PROJECT/")

app = Flask(__name__)
CrashlensFlask(app)
```

Application-factory style:

```python
import crashlens.flask

def create_app():
    app = Flask(__name__)
    crashlens.flask.init_app(app)
    return app
```

Unhandled request exceptions are reported with the request URL and method.

## FastAPI and Starlette

```python
from fastapi import FastAPI
from crashlens.asgi import CrashlensMiddleware
import crashlens

crashlens.init("https://KEY@host/api/ingest/PROJECT/")

app = FastAPI()
app.add_middleware(CrashlensMiddleware)
```

Or wrap any ASGI app directly:

```python
app = CrashlensMiddleware(app)
```

The middleware captures the exception, adds the request URL and method, and
re-raises so your framework's own error handling still runs.

## Logging

Send log records at or above a level as events:

```python
import logging
from crashlens.logging import CrashlensHandler
import crashlens

crashlens.init("https://KEY@host/api/ingest/PROJECT/")

logging.getLogger().addHandler(CrashlensHandler(level=logging.ERROR))

logging.error("checkout failed")          # sent as an error event
logging.exception("unexpected failure")   # sent with the exception attached
```

Optionally record lower-level log lines as breadcrumbs so the trail leading up
to an error travels with it:

```python
from crashlens.logging import CrashlensBreadcrumbHandler

logging.getLogger().addHandler(CrashlensBreadcrumbHandler(level=logging.INFO))
```

## How sending works

- Events go through a bounded in-memory queue drained by one background daemon
  thread, one event per request.
- When the queue is full, the newest event is dropped (with a one-time warning)
  rather than blocking your application.
- Bodies larger than 4 KB are gzip-compressed.
- On a connection error or a server 5xx, the SDK retries once, then drops. On a
  429 it backs off briefly and drops. It never retries forever and never raises
  into your application.
- `crashlens.flush()` and an automatic hook at process exit give queued events a
  chance to send.

## Development

```bash
cd sdks/python
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
./.venv/bin/pytest
```

Note: the PyPI name `crashlens` may already be taken. Nothing here is published;
install from source for now.
