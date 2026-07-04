# SDKs

Crashlens ships three client SDKs. Pick the one matching where your errors
happen; you can use more than one against the same Crashlens instance (for
example the Node SDK on your API and the browser SDK on your frontend), each
with its own project and its own DSN key.

- [Python](../sdks/python/README.md) - plain Python, Flask, FastAPI /
  Starlette, and a `logging` handler.
- [Browser](../sdks/browser/README.md) - a `<script>` tag or an npm / ESM
  import.
- [Node](../sdks/node/README.md) - plain Node.js or Express middleware.

## Getting your DSN

Every project has its own key. In the dashboard: sign in, open your
organization, open the project, and either use an existing key or click
**Create key**. The project page then shows the ingest endpoint and the key
side by side under **Connect your app**.

All three SDKs accept the pieces two ways:

**A single DSN string**, in the form:

```
https://<public_key>@<your-crashlens-host>/api/ingest/<project_id>/
```

The public key is the non-secret half of a DSN (it identifies and
authenticates the project, but reveals nothing your app doesn't already
expose by sending events) and is safe to ship inside a browser bundle.

```python
crashlens.init("https://YOUR_KEY@your-host/api/ingest/YOUR_PROJECT_ID/")
```

```js
init({ dsn: "https://YOUR_KEY@your-host/api/ingest/YOUR_PROJECT_ID/" });
```

**Or the endpoint and key as two separate values**, if you would rather not
embed the key inside a URL (for example to keep it in its own config
variable):

```python
crashlens.init(url="https://your-host/api/ingest/YOUR_PROJECT_ID/", key="YOUR_KEY")
```

```js
init({ url: "https://your-host/api/ingest/YOUR_PROJECT_ID/", key: "YOUR_KEY" });
```

The dashboard's install snippet shows the endpoint and the key as two
separate values for exactly this reason: combine them into a DSN yourself,
or pass them straight through as `url` and `key`.

## Comparison

| | Python | Browser | Node |
| --- | --- | --- | --- |
| Package | `crashlens` (install from source: `pip install -e sdks/python`) | `@crashlens/browser` | `@crashlens/node` |
| Runtime dependencies | none (standard library only) | none | none (Node builtins only) |
| Size | n/a (Python package) | under 4 KB gzipped | n/a (Node package) |
| Auto-captures | unhandled exceptions (`sys.excepthook`) | `window.onerror`, unhandled promise rejections | `uncaughtException`, `unhandledRejection` |
| Framework integrations | Flask, FastAPI/Starlette (ASGI middleware), `logging` handler | none beyond the base install | Express request/error middleware |
| Delivery | background daemon thread, bounded queue | `fetch` with `keepalive`, `navigator.sendBeacon` fallback | background sender, bounded queue |
| Breadcrumbs | manual + log-handler breadcrumbs, per-context via `contextvars` | automatic navigation breadcrumbs, opt-in console breadcrumbs | manual, per-request isolated via `AsyncLocalStorage` |
| Release / environment tags | yes | yes | yes |
| `beforeSend` scrub/drop hook | no (not in this SDK) | yes | yes |

See each SDK's own README for its full options table, framework wiring, and
notes on what it does not yet do.
