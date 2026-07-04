// Event transport. A tiny in-memory queue feeds sequential sends over fetch
// (with keepalive for small payloads). sendBeacon is the fallback when fetch is
// unavailable and for the pagehide / visibilitychange flush.
//
// FLAGGED DEFAULTS (governor review):
//   - MAX_QUEUE = 30, drop-oldest on overflow.
//   - KEEPALIVE_MAX_BYTES = 60 KB; larger payloads still use fetch but without
//     the keepalive flag (browsers cap keepalive bodies near 64 KB).
//   - MAX_RETRY_MS = 5000: a 429 Retry-After beyond 5s drops the event.
//
// KNOWN LIMITATION: navigator.sendBeacon cannot set request headers, so it
// cannot send X-Crashlens-Key. Beacon delivery therefore only authenticates if
// the server later accepts the key as a query parameter; today it is a
// best-effort last resort. The keepalive fetch path (which DOES carry the
// header and survives page unload) is the primary flush mechanism. FLAGGED.

import type { CrashlensEvent } from "./types";

const MAX_QUEUE = 30;
const KEEPALIVE_MAX_BYTES = 60 * 1024;
const MAX_RETRY_MS = 5000;

interface TransmitResult {
  retryAfterMs?: number;
}

export class Transport {
  private readonly url: string;
  private readonly key: string;
  private readonly maxQueue: number;
  private queue: string[] = [];
  private sending = false;
  private closed = false;

  constructor(url: string, key: string, maxQueue: number = MAX_QUEUE) {
    this.url = url;
    this.key = key;
    this.maxQueue = maxQueue;
  }

  send(event: CrashlensEvent): void {
    if (this.closed) return;
    let body: string;
    try {
      body = JSON.stringify(event);
    } catch {
      return;
    }
    this.enqueue(body);
    void this.drain();
  }

  // Best-effort synchronous flush for pagehide / visibilitychange. Prefers a
  // keepalive fetch (carries the auth header, survives unload) and only uses
  // sendBeacon when fetch is unavailable.
  flush(): void {
    const pending = this.queue;
    this.queue = [];
    for (const body of pending) {
      if (typeof fetch === "function") {
        try {
          void fetch(this.url, this.fetchInit(body, true)).catch(() =>
            this.beacon(body),
          );
        } catch {
          this.beacon(body);
        }
      } else {
        this.beacon(body);
      }
    }
  }

  close(): void {
    this.closed = true;
    this.queue = [];
  }

  // Exposed for tests.
  queueLength(): number {
    return this.queue.length;
  }

  private enqueue(body: string): void {
    this.queue.push(body);
    while (this.queue.length > this.maxQueue) {
      this.queue.shift(); // drop oldest
    }
  }

  private async drain(): Promise<void> {
    if (this.sending) return;
    this.sending = true;
    try {
      while (this.queue.length > 0 && !this.closed) {
        const body = this.queue[0];
        const result = await this.transmit(body);
        if (
          result.retryAfterMs !== undefined &&
          result.retryAfterMs > 0 &&
          result.retryAfterMs <= MAX_RETRY_MS
        ) {
          await delay(result.retryAfterMs);
          // One retry; the outcome is ignored and the event is dropped either
          // way so the queue keeps draining.
          await this.transmit(body);
        }
        this.queue.shift();
      }
    } catch {
      // Never let a transport failure escape into the host page.
    } finally {
      this.sending = false;
    }
  }

  private fetchInit(body: string, keepalive: boolean): RequestInit {
    return {
      method: "POST",
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "X-Crashlens-Key": this.key,
      },
      body,
      keepalive,
    };
  }

  private async transmit(body: string): Promise<TransmitResult> {
    if (typeof fetch !== "function") {
      this.beacon(body);
      return {};
    }
    const keepalive = byteLength(body) < KEEPALIVE_MAX_BYTES;
    try {
      const res = await fetch(this.url, this.fetchInit(body, keepalive));
      if (res.status === 429) {
        return {
          retryAfterMs: parseRetryAfter(res.headers.get("Retry-After")),
        };
      }
      return {};
    } catch {
      // Network error: fall back to a beacon so the event is not simply lost.
      this.beacon(body);
      return {};
    }
  }

  private beacon(body: string): boolean {
    if (
      typeof navigator === "undefined" ||
      typeof navigator.sendBeacon !== "function"
    ) {
      return false;
    }
    try {
      const blob = new Blob([body], { type: "application/json" });
      return navigator.sendBeacon(this.url, blob);
    } catch {
      return false;
    }
  }
}

function parseRetryAfter(header: string | null): number {
  if (!header) return 0;
  const secs = parseInt(header, 10);
  return Number.isFinite(secs) && secs > 0 ? secs * 1000 : 0;
}

function byteLength(s: string): number {
  if (typeof TextEncoder !== "undefined") {
    return new TextEncoder().encode(s).length;
  }
  return s.length;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
