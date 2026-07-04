# Crashlens Browser SDK (stub)

Client SDK for reporting errors from browser JavaScript applications to a
Crashlens instance. Not implemented yet; this directory is a placeholder.

Planned scope:

- Automatic capture of `window.onerror` and `unhandledrejection`.
- A manual `capture()` call.
- Breadcrumbs.
- A small bundle with no runtime dependencies.

The event payload this SDK will send is specified in
[`../../docs/PROTOCOL.md`](../../docs/PROTOCOL.md) (DRAFT).
