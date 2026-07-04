# Crashlens Node SDK (stub)

Client SDK for reporting errors from Node.js applications to a Crashlens
instance. Not implemented yet; this directory is a placeholder.

Planned scope:

- Automatic capture of `uncaughtException` and `unhandledRejection`.
- Express middleware.
- A background, non-blocking sender.

The event payload this SDK will send is specified in
[`../../docs/PROTOCOL.md`](../../docs/PROTOCOL.md) (DRAFT).
