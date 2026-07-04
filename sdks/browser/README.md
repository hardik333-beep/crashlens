# @crashlens/browser

Tiny, zero-dependency browser JavaScript SDK for [Crashlens](../../README.md),
the self-hosted error monitoring service. Drop in a script tag or one import,
call `init` once, and uncaught browser errors show up in your Crashlens
dashboard.

- No runtime dependencies.
- Automatic capture of uncaught errors and unhandled promise rejections.
- Automatic navigation breadcrumbs, plus opt-in console breadcrumbs.
- Stack traces normalised to the Crashlens protocol (Chrome, Edge, Firefox,
  and Safari formats supported).
- Under 4 KB gzipped.

## Install

### Script tag (no build step)

```html
<script src="https://cdn.example.com/crashlens.iife.js"></script>
<script>
  Crashlens.init({
    dsn: "https://YOUR_PUBLIC_KEY@errors.your-host.com/api/ingest/YOUR_PROJECT_ID/",
  });
</script>
```

The IIFE build exposes a global named `Crashlens`. Load it as early as possible
in the page `<head>` so it can catch errors that happen during startup.

### npm / ESM

```bash
npm install @crashlens/browser
```

```js
import { init, captureException, captureMessage } from "@crashlens/browser";

init({
  dsn: "https://YOUR_PUBLIC_KEY@errors.your-host.com/api/ingest/YOUR_PROJECT_ID/",
});
```

## The DSN

Your project's DSN is a single URL that carries both the ingest endpoint and the
public key:

```
https://<public_key>@<your-crashlens-host>/api/ingest/<project_id>/
```

The public key is the non-secret half of the DSN and is safe to ship in a
browser bundle. If you would rather pass the pieces separately, use `url` and
`key` instead of `dsn`:

```js
init({
  url: "https://errors.your-host.com/api/ingest/YOUR_PROJECT_ID/",
  key: "YOUR_PUBLIC_KEY",
});
```

## Options

```js
init({
  dsn: "...",                  // or url + key
  environment: "production",   // default "production"
  release: "web@1.4.2",        // optional release identifier
  maxBreadcrumbs: 100,         // ring buffer size, default 100
  consoleBreadcrumbs: false,   // opt in to console.warn/error breadcrumbs
  beforeSend: (event) => event // scrub or drop events, see below
});
```

`init` is idempotent: calling it a second time is a no-op. Call `close()` first
if you need to re-initialise.

## Manual API

```js
import {
  captureException,
  captureMessage,
  addBreadcrumb,
  setTag,
  setUser,
  close,
} from "@crashlens/browser";

try {
  doRiskyThing();
} catch (err) {
  captureException(err);
}

captureMessage("Checkout completed", "info");

addBreadcrumb({ category: "ui", message: "Clicked pay", level: "info" });

setTag("feature", "checkout");
setUser({ id: "user-42" });

close(); // uninstall handlers and stop reporting
```

Every function is safe to call before `init`; it simply does nothing until the
SDK is configured. No SDK call ever throws into your page.

## Scrubbing with beforeSend

`beforeSend` runs on every event just before it is sent. Return a modified event
to scrub it, or return `null` to drop it entirely.

```js
init({
  dsn: "...",
  beforeSend: (event) => {
    // Drop events from a noisy third-party script.
    if (event.exception?.value?.includes("ext-widget")) {
      return null;
    }
    // Redact anything that looks like an email in the message.
    if (event.message) {
      event.message = event.message.replace(/[^\s@]+@[^\s@]+/g, "[email]");
    }
    return event;
  },
});
```

## What is captured automatically

- `window.onerror` (uncaught exceptions). Any handler you already installed is
  preserved and still called.
- `window.onunhandledrejection` (unhandled promise rejections). Error reasons
  keep their stack trace; non-Error reasons become messages.
- Navigation breadcrumbs from `history.pushState`, `history.replaceState`, and
  `popstate`.
- Console breadcrumbs from `console.warn` and `console.error`, but only when you
  set `consoleBreadcrumbs: true`. Only the first argument is recorded, truncated
  to 200 characters.

Network requests (fetch / XHR) are not instrumented in this version to keep the
bundle small. That is a planned future option.

## Notes and limitations

- Stack frames are marked `in_app: true` at this version. Source maps and real
  in_app inference land in a later release.
- Delivery uses `fetch` with `keepalive` for small payloads, so events sent
  right before a page unload still go out. `navigator.sendBeacon` is used as a
  fallback when `fetch` is unavailable.

## Development

```bash
npm install
npm test        # vitest + jsdom
npm run build   # esbuild -> dist/, tsc -> dist/index.d.ts
npm run typecheck
```

## License

MIT.
