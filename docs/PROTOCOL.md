# Crashlens ingest protocol - v1 event envelope

Status: v1 - FROZEN for v1 SDK development, 2026-07-04.

This document specifies the JSON payload that Crashlens SDKs send to a Crashlens
instance, and the HTTP contract of the ingest endpoint. All questions raised in
the draft have been resolved; the rulings are recorded inline as normative text
and summarised in section 5.

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
- Body encoding: UTF-8 JSON. Clients MAY send gzip-compressed bodies with
  `Content-Encoding: gzip`. The edge body cap applies to the compressed bytes.
  The application additionally enforces a 1 MB decompressed-size cap and
  returns `413` above it, as a decompression bomb guard.
- Body size: capped at the edge (about 250 KB). Oversized requests get `413`.

## 2. Response semantics

The ingest endpoint never does grouping or storage inline; it authenticates,
validates shape, enqueues, and returns quickly.

- `202 Accepted` - the event passed authentication and basic shape validation
  and has been accepted for asynchronous processing. Acceptance does not
  guarantee the event survives later processing (for example it may be dropped
  by sampling).
- `400 Bad Request` - the JSON is malformed or a required field is missing or
  the wrong type.
- `401 Unauthorized` - the `X-Crashlens-Key` header is missing or invalid.
- `403 Forbidden` - the key is valid but not permitted for the `{project_id}`
  in the path.
- `413 Payload Too Large` - the request body exceeds the configured cap
  (compressed at the edge, or 1 MB decompressed in the application).
- `429 Too Many Requests` - the per-DSN rate limit has been exceeded. Includes a
  `Retry-After` header (seconds). Clients should back off and may drop events.
- `202` acknowledgement body shape: `{ "id": "<event_id echoed>" }`. The server
  always echoes the client-supplied `event_id`.

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
          "context_line": "    return subtotal / count",
          "in_app": true
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

Unknown top-level fields are IGNORED by the server (forward compatible).

### 3.2 Message or exception

At least one of `message` or `exception` must be present. A pure log-style
event carries `message` and no `exception`; a captured crash carries
`exception` (and may also carry `message`). An event with neither is rejected
with `400`.

### 3.3 Exception object

```
exception = {
  "type":  string,           // required, for example "ZeroDivisionError"
  "value": string,           // required, the exception message
  "stacktrace": {
    "frames": [ frame, ... ] // required array, may be empty, ordered
  },
  "cause": exception         // optional, recursive "caused by" chain, see below
}

frame = {
  "filename":     string,   // required
  "function":     string,   // required
  "lineno":       integer,  // required
  "colno":        integer,  // required
  "context_line": string,   // optional, the source line at lineno
  "in_app":       boolean,  // optional, default true; marks application code
                            // vs library code. Grouping quality depends on it.
  "pre_context":  [string], // optional, source lines before lineno, max 5
  "post_context": [string]  // optional, source lines after lineno, max 5
}
```

Frame ordering is canonical across all platforms: oldest call first, crash
frame last (Python's natural order). SDKs normalise to this order before
sending; the server never reorders.

Chained exceptions: v1 supports a single `exception` object with an optional
recursive `cause` field carrying the same exception shape, to a maximum depth
of 5. Deeper chains are truncated server-side. Fingerprinting uses the root
cause (the deepest exception in the chain).

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

The server keeps the most recent 100 breadcrumbs per event and silently drops
older ones.

## 4. Limits and validation

- String limits, enforced server-side by silent truncation with a trailing
  `...` marker:
  - `message`: 8192 characters.
  - tag keys: 32 characters.
  - tag values: 200 characters.
  - `filename` and `function`: 256 characters.
  - each context line (`context_line` and every entry of `pre_context` /
    `post_context`): 256 characters.
- Maximum 128 frames per stacktrace. On overflow the server keeps the LAST 128
  frames (nearest the crash under the canonical ordering).
- Unknown top-level fields are ignored (forward compatible).
- Version negotiation: the path is the only version signal. The current
  endpoint is implicitly v1; a breaking change ships a new path. Additive
  evolution relies on unknown fields being ignored.

## 5. Rulings log

Resolutions to the draft's open questions, ruled by the governor 2026-07-04.
Each also appears inline above as normative text.

1. gzip request bodies: ACCEPTED at v1. Edge cap applies to compressed bytes;
   the application enforces a 1 MB decompressed cap and returns `413` above it.
2. `202` acknowledgement body: `{ "id": "<event_id echoed>" }`, always echoing
   the client event_id.
3. CONFIRMED: at least one of `message` or `exception` must be present.
4. Canonical frame order: oldest call first, crash frame last. SDKs normalise
   before sending; the server never reorders.
5. `in_app` (boolean, optional, default true) is part of v1 frames.
   `pre_context` / `post_context` are accepted optional arrays, max 5 lines
   each.
6. Chained exceptions: single `exception` with an optional recursive `cause`
   of the same shape, max depth 5; deeper chains truncated server-side.
   Fingerprinting uses the root cause.
7. Breadcrumbs: the server keeps the most recent 100 per event and silently
   drops older ones.
8. String limits with silent truncation and a trailing `...` marker: `message`
   8192; tag keys 32; tag values 200; `filename` and `function` 256; each
   context line 256.
9. Max 128 frames per stacktrace; on overflow the server keeps the LAST 128.
10. Unknown top-level fields are IGNORED (forward compatible).
11. Version negotiation: the path is the only version signal; the current
    endpoint is implicitly v1, and a breaking change ships a new path.
