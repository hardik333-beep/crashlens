// Event transport. A bounded array queue feeds a single in-flight send loop.
// One event is POSTed per request over node:http / node:https.
//
// Design (matches the sibling SDKs and docs/PROTOCOL.md):
//   - The host app never blocks: send() enqueues and returns; the drain loop
//     runs on its own.
//   - Bounded queue: at maxQueue the OLDEST event is dropped (drop-oldest).
//   - gzip the body when it exceeds GZIP_THRESHOLD (4 KiB).
//   - 2s request timeout.
//   - On a socket error or 5xx, retry ONCE after RETRY_DELAY_MS, then drop.
//   - On 429, wait min(Retry-After, MAX_RETRY_AFTER_MS) then drop (no resend).
//   - Nothing here ever throws into the host application.

import * as http from "node:http";
import * as https from "node:https";
import { gzipSync } from "node:zlib";
import type { CrashlensEvent } from "./types";
import { delay } from "./util";

export const GZIP_THRESHOLD = 4096; // 4 KiB
export const RETRY_DELAY_MS = 500;
export const MAX_RETRY_AFTER_MS = 5000;
export const DEFAULT_TIMEOUT_MS = 2000;
export const DEFAULT_MAX_QUEUE = 100;

const SDK_USER_AGENT = "crashlens-node";

interface PostResult {
  status: number;
  retryAfterMs: number;
}

export class Transport {
  private readonly url: string;
  private readonly key: string;
  private readonly maxQueue: number;
  private readonly timeoutMs: number;
  private readonly lib: typeof http | typeof https;
  private queue: string[] = [];
  private sending = false;
  private closed = false;
  private warnedFull = false;

  constructor(
    url: string,
    key: string,
    maxQueue: number = DEFAULT_MAX_QUEUE,
    timeoutMs: number = DEFAULT_TIMEOUT_MS,
  ) {
    this.url = url;
    this.key = key;
    this.maxQueue = Math.max(1, maxQueue);
    this.timeoutMs = timeoutMs;
    this.lib = url.startsWith("https:") ? https : http;
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

  // Drain the queue with a deadline. Returns true if the queue emptied.
  async flush(timeoutMs = 5000): Promise<boolean> {
    const deadline = Date.now() + timeoutMs;
    void this.drain();
    while (this.queue.length > 0 && !this.closed && Date.now() < deadline) {
      await delay(20);
    }
    return this.queue.length === 0;
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
      if (!this.warnedFull) {
        this.warnedFull = true;
        // A single, quiet warning so the drop is discoverable without noise.
        if (typeof process !== "undefined" && process.emitWarning) {
          process.emitWarning(
            "crashlens: event queue is full; dropping oldest events",
          );
        }
      }
    }
  }

  private async drain(): Promise<void> {
    if (this.sending) return;
    this.sending = true;
    try {
      while (this.queue.length > 0 && !this.closed) {
        const body = this.queue[0];
        await this.transmit(body);
        this.queue.shift();
      }
    } catch {
      // Never let a transport failure escape into the host app.
    } finally {
      this.sending = false;
    }
  }

  private prepare(body: string): {
    data: Buffer;
    headers: Record<string, string | number>;
  } {
    let data = Buffer.from(body, "utf-8");
    const headers: Record<string, string | number> = {
      "Content-Type": "application/json; charset=utf-8",
      "X-Crashlens-Key": this.key,
      "User-Agent": SDK_USER_AGENT,
    };
    if (data.length > GZIP_THRESHOLD) {
      data = gzipSync(data);
      headers["Content-Encoding"] = "gzip";
    }
    headers["Content-Length"] = data.length;
    return { data, headers };
  }

  private async transmit(body: string): Promise<void> {
    const { data, headers } = this.prepare(body);
    for (const attempt of [0, 1]) {
      let result: PostResult;
      try {
        result = await this.post(data, headers);
      } catch {
        // Socket-level failure: retry once, then drop.
        if (attempt === 0) {
          await delay(RETRY_DELAY_MS);
          continue;
        }
        return;
      }
      if (result.status === 429) {
        const wait = Math.min(result.retryAfterMs, MAX_RETRY_AFTER_MS);
        if (wait > 0) await delay(wait);
        return; // drop, never resend on 429
      }
      if (result.status >= 500 && result.status < 600) {
        if (attempt === 0) {
          await delay(RETRY_DELAY_MS);
          continue;
        }
        return;
      }
      // 2xx success or a 4xx we cannot fix: done either way.
      return;
    }
  }

  private post(
    data: Buffer,
    headers: Record<string, string | number>,
  ): Promise<PostResult> {
    return new Promise<PostResult>((resolve, reject) => {
      const req = this.lib.request(
        this.url,
        { method: "POST", headers },
        (res) => {
          const retryAfterMs = parseRetryAfter(res.headers["retry-after"]);
          res.on("data", () => {
            // Drain the body so the socket can be reused / freed.
          });
          res.on("end", () => {
            resolve({ status: res.statusCode ?? 0, retryAfterMs });
          });
          res.on("error", reject);
        },
      );
      req.on("error", reject);
      req.setTimeout(this.timeoutMs, () => {
        req.destroy(new Error("crashlens: request timed out"));
      });
      req.write(data);
      req.end();
    });
  }
}

function parseRetryAfter(header: string | string[] | undefined): number {
  if (!header) return 0;
  const value = Array.isArray(header) ? header[0] : header;
  const secs = parseInt(value, 10);
  return Number.isFinite(secs) && secs > 0 ? secs * 1000 : 0;
}
