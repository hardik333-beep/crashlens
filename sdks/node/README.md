# Crashlens Node SDK

A tiny, zero-dependency Node.js SDK for [Crashlens](../../README.md), the
open-source self-hosted error monitoring tool. One import and one `init` line
and unhandled errors from your Node service show up in your Crashlens
dashboard.

- Zero runtime dependencies (Node builtins only: `http`/`https`, `zlib`,
  `async_hooks`, `crypto`).
- Automatic capture of `uncaughtException` and `unhandledRejection`.
- Express middleware for per-request context and error capture.
- Per-request context isolation via `AsyncLocalStorage`, so concurrent requests
  never share tags or breadcrumbs.
- Non-blocking background sender that never throws into your app.

## Install

```
npm install @crashlens/node
```

Requires Node 18 or newer.

## Get your DSN

Your Crashlens project gives you a DSN in this form:

```
https://<public_key>@your-crashlens-host.example.com/api/ingest/<project_id>/
```

The public key is safe to keep in your server config. You can pass the DSN, or
pass the ingest `url` and `key` separately.

## Quick start (plain Node)

```js
import { init, captureException, captureMessage } from "@crashlens/node";

init({
  dsn: "https://PUBLIC_KEY@your-host.example.com/api/ingest/PROJECT_ID/",
  environment: "production",
  release: "my-service@1.4.2",
});

// Unhandled errors and promise rejections are now reported automatically.

// You can also report things yourself:
try {
  doWork();
} catch (err) {
  captureException(err);
}

captureMessage("worker started", "info");
```

CommonJS works too:

```js
const { init } = require("@crashlens/node");
init({ dsn: process.env.CRASHLENS_DSN });
```

## Quick start (Express)

Mount the request handler before your routes and the error handler after them.

```js
import express from "express";
import {
  init,
  crashlensRequestHandler,
  crashlensErrorHandler,
} from "@crashlens/node";

init({ dsn: process.env.CRASHLENS_DSN });

const app = express();

// Runs each request in its own scope so tags and breadcrumbs stay isolated.
app.use(crashlensRequestHandler());

app.get("/invoices/:id", (req, res) => {
  res.send("ok");
});

// Captures unhandled route errors with the request url and method, then passes
// the error on to your own error handling.
app.use(crashlensErrorHandler());

app.listen(3000);
```

## Adding context

```js
import { setTag, setUser, addBreadcrumb } from "@crashlens/node";

setUser({ id: "user-123" });
setTag("transaction", "POST /invoices");
addBreadcrumb({ category: "http", message: "GET /invoices/17", level: "info" });
```

Inside an Express request (with `crashlensRequestHandler()` mounted) this
context is scoped to that request only, so two requests handled at the same time
never see each other's tags or breadcrumbs.

## Configuration

`init(options)` accepts:

| Option | Default | Meaning |
| --- | --- | --- |
| `dsn` | none | The full DSN. Or supply `url` + `key` instead. |
| `url` + `key` | none | The ingest URL and public key, if not using a DSN. |
| `environment` | `"production"` | Environment name attached to every event. |
| `release` | none | Release identifier attached to every event. |
| `inAppPathPrefixes` | none | Path prefixes marking your code. When set, a stack frame is treated as in-app if and only if its file path starts with one of these prefixes (the node_modules heuristic is ignored). |
| `maxQueue` | `100` | Buffer size before the oldest queued event is dropped. |
| `beforeSend` | none | Hook to scrub or drop an event. Return `null` to drop it. |
| `captureUncaughtException` | `true` | Auto-capture uncaught exceptions. |
| `captureUnhandledRejection` | `true` | Auto-capture unhandled promise rejections. |

`init` is idempotent: calling it twice does nothing the second time. Call
`close()` first if you need to re-init.

## Uncaught exception behaviour

When an exception reaches `process.on("uncaughtException")`, this SDK captures
it (as a `fatal` event) and best-effort flushes the queue. It then mirrors
Node's own default honestly:

- If your app has no other `uncaughtException` listener, the SDK reproduces
  Node's default after flushing: it prints the error to stderr and exits with
  code 1.
- If your app has its own `uncaughtException` listener, the SDK does nothing
  extra to the process. It captures and flushes, and leaves the exit decision
  to you.

Unhandled promise rejections are captured but never change your process
behaviour.

## Graceful shutdown

```js
import { flush, close } from "@crashlens/node";

// Wait up to 5s for buffered events to send.
await flush(5000);

// Or flush, remove the process hooks, and tear down.
await close();
```

## Transport behaviour

- One event per POST to `POST {url}` with the `X-Crashlens-Key` header.
- Bodies larger than 4 KiB are gzip-compressed.
- 2 second request timeout.
- One retry after a socket error or `5xx`, then the event is dropped.
- On `429`, the SDK waits up to the `Retry-After` value (capped at 5s) and then
  drops the event.
- The queue is bounded; the oldest event is dropped on overflow.
- Nothing in the transport ever throws into your application.

## License

MIT.
