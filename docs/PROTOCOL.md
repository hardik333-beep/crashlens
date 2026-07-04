# Crashlens ingest protocol - v1 event envelope (DRAFT)

Status: DRAFT. This is a proposal for a governor to review before any SDK is
built. Nothing here is final. Every unresolved point is marked with `OPEN:`.

This document specifies the JSON payload that Crashlens SDKs send to a Crashlens
instance, and the HTTP contract of the ingest endpoint.

## 1. Transport

- Method and path: `POST /api/ingest/{project_id}/`
  - `project_id` identifies the receiving project. The reverse proxy strips the
    `/api` prefix, so the application sees `POST /ingest/{project_id}/`.
- Authentication header: `X-Crashlens-Key: <dsn_public_key>`
  - The DSN public key is the non-secret half of a project's DSN. It identifies
    and authenticates the sending client to a specific project. It is safe to
    embed in browser bundles.
  - The `{project_id}` in the path and the project bound to the key must match,
    or the request is rejected.
- Content type: `application/json; charset=utf-8`.
- Body encoding: UTF-8 JSON. `OPEN:` do we also accept gzip-compressed bodies
  (`Content-Encoding: gzip`) at v1, given the edge body cap is measured on the
  compressed bytes?
- Body size: capped at the edge (about 250 KB). Oversized requests get `413`.

## 2. Response semantics

The ingest endpoint never does grouping or storage inline; it authenticates,
validates shape, enqueues, and returns quickly.

- `202 Accepted` - the event passed authentication and basic shape validation
  and has been accepted for asynchronous processing. The body is a small JSON
  acknowledgement. Acceptance does not guarantee the event survives later
  processing (for example it may be dropped by sampling).
- `400 Bad Request` - the JSON is malformed or a required field is missing or
  the wrong type.
- `401 Unauthorized` - the `X-Crashlens-Key` header is missing or invalid.
- `403 Forbidden` - the key is valid but not permitted for the `{project_id}`
  in the path.
- `413 Payload Too Large` - the request body exceeds the configured cap.
- `429 Too Many Requests` - the per-DSN rate limit has been exceeded. Includes a
  `Retry-After` header (seconds). Clients should back off and may drop events.
- `202` acknowledgement body shape: `{ "id": "<event_id echoed>" }`.
  `OPEN:` should the acknowledgement echo the event id, return a server-assigned
  id, or return an empty body for minimum overhead?

## 3. Event envelope

A single event is one JSON object. `?` on a field name marks it as optional.

```json
{
  "event_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "timestamp": "2026-07-04T12:00:00.000Z",
  "platform": "python",
  "level": "error",
  "message": "Division by zero in invoice total",
  "exception": {
    "type": "ZeroDivisionError",
    "value": "division by zero",
    "stacktrace": {
      "frames": [
        {
          "filename": "app/billing/invoice.py",
          "function": "compute_total",
          "lineno": 42,
          "colno": 12,
          "context_line": "    return subtotal / count"
        }
      ]
    }
  },
  "breadcrumbs": [
    {
      "timestamp": "2026-07-04T11:59:58.100Z",
      "type": "navigation",
      "category": "http",
      "level": "info",
      "message": "GET /invoices/17",
      "data": {}
    }
  ],
  "tags": {
    "server_name": "web-1",
    "transaction": "POST /invoices"
  },
  "environment": "production",
  "release": "web@1.4.2",
  "sdk": {
    "name": "crashlens-python",
    "version": "0.1.0"
  },
  "user": {
    "id": "user-123"
  },
  "request": {
    "url": "https://app.example.com/invoices/17",
    "method": "POST"
  }
}
```

### 3.1 Field reference

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `event_id` | string (UUID) | yes | Client-generated UUID. Used for idempotent processing (a repeat of the same id is processed once). |
| `timestamp` | string (RFC3339) | yes | When the event occurred, UTC recommended. |
| `platform` | string | yes | For example `python`, `javascript`, `node`. |
| `level` | string | yes | One of `fatal`, `error`, `warning`, `info`, `debug`. |
| `message` | string | no | Human-readable message. Required only when `exception` is absent (see 3.2). |
| `exception` | object | no | Present for captured exceptions. See 3.3. |
| `breadcrumbs` | array | no | Ordered trail of prior events. Defaults to empty. See 3.4. |
| `tags` | object (string to string) | no | Indexed key/value pairs for filtering. |
| `environment` | string | yes | For example `production`, `staging`. |
| `release` | string | no | Release identifier the event belongs to. |
| `sdk` | object | yes | `{ "name": string, "version": string }`. |
| `user` | object | no | `{ "id"?: string }`. |
| `request` | object | no | `{ "url"?: string, "method"?: string }`. |

### 3.2 Message or exception

At least one of `message` or `exception` must be present. A pure log-style
event carries `message` and no `exception`; a captured crash carries
`exception` (and may also carry `message`).
`OPEN:` confirm this "at least one of" rule, versus always requiring `message`.

### 3.3 Exception object

```
exception = {
  "type":  string,          // required, for example "ZeroDivisionError"
  "value": string,          // required, the exception message
  "stacktrace": {
    "frames": [ frame, ... ] // required array, may be empty, ordered
  }
}

frame = {
  "filename":     string,   // required
  "function":     string,   // required
  "lineno":       integer,  // required
  "colno":        integer,  // required
  "context_line": string    // optional, the source line at lineno
}
```

`OPEN:` frame ordering convention - the proposal is oldest call first, crash
frame last (Python's natural order). Browser and Node stacks are typically the
reverse; the SDKs must normalise to one server-side convention. Which one is
canonical?
`OPEN:` should a frame carry `in_app` (a boolean marking application vs library
code) and `pre_context` / `post_context` (surrounding source lines) at v1, or
are these deferred to a later version?
`OPEN:` do we support chained exceptions (a list of exception objects for
"caused by" chains) at v1, or only a single exception?

### 3.4 Breadcrumb object

```
breadcrumb = {
  "timestamp": string (RFC3339), // required
  "type":      string,           // optional, for example "navigation", "http"
  "category":  string,           // optional
  "level":     string,           // optional, same set as event level
  "message":   string,           // optional
  "data":      object            // optional, free-form string keyed map
}
```

`OPEN:` maximum number of breadcrumbs accepted per event, and what the server
does past that limit (truncate oldest vs reject). Proposal: keep the most recent
100, silently truncating older ones.

## 4. Limits and validation

- `OPEN:` maximum length for `message`, `tags` keys and values, and `filename`
  / `function` strings before server-side truncation.
- `OPEN:` maximum frame count per stacktrace.
- `OPEN:` whether unknown top-level fields are ignored (forward-compatible) or
  rejected. Proposal: ignore unknown fields.
- `OPEN:` version negotiation - do we send an explicit envelope version (for
  example a `v` field or an `X-Crashlens-Protocol-Version` header) so the server
  can evolve the schema, or is the endpoint path the only version signal?

## 5. Open questions summary

Collected here for the governor. Each also appears inline above.

1. `OPEN:` accept gzip-compressed request bodies at v1?
2. `OPEN:` `202` acknowledgement body shape (echo id, server id, or empty)?
3. `OPEN:` confirm the "message or exception, at least one" rule.
4. `OPEN:` canonical stack frame ordering across platforms.
5. `OPEN:` include `in_app`, `pre_context`, `post_context` on frames at v1?
6. `OPEN:` support chained ("caused by") exceptions at v1?
7. `OPEN:` breadcrumb cap and truncation behaviour (proposed 100, drop oldest).
8. `OPEN:` string length limits and truncation policy.
9. `OPEN:` maximum frame count per stacktrace.
10. `OPEN:` ignore vs reject unknown fields (proposed ignore).
11. `OPEN:` protocol version negotiation mechanism.
