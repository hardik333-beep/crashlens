import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { Transport } from "../src/transport";
import type { CrashlensEvent } from "../src/types";
import { startServer, type TestServer } from "./helpers";

function event(id: string, message = "boom"): CrashlensEvent {
  return {
    event_id: id,
    timestamp: "2026-07-04T12:00:00.000Z",
    platform: "node",
    level: "error",
    message,
    environment: "production",
    sdk: { name: "crashlens-node", version: "0.1.0" },
  };
}

let server: TestServer;

beforeEach(async () => {
  server = await startServer();
});

afterEach(async () => {
  await server.close();
});

describe("Transport - happy path", () => {
  it("POSTs one event per request with the key header and full envelope", async () => {
    const url = `http://127.0.0.1:${server.port}/api/ingest/proj-1/`;
    const t = new Transport(url, "pubkey");
    t.send(event("id-1"));
    t.send(event("id-2"));
    await server.waitFor(2);

    expect(server.requests).toHaveLength(2);
    for (const r of server.requests) {
      expect(r.method).toBe("POST");
      expect(r.url).toBe("/api/ingest/proj-1/");
      expect(r.headers["x-crashlens-key"]).toBe("pubkey");
      expect(String(r.headers["content-type"])).toContain("application/json");
    }
    const ids = server.requests.map((r) => (r.body as CrashlensEvent).event_id);
    expect(ids).toEqual(["id-1", "id-2"]);
    expect((server.requests[0].body as CrashlensEvent).platform).toBe("node");
  });
});

describe("Transport - gzip", () => {
  it("gzip-compresses bodies larger than the 4 KiB threshold", async () => {
    const url = `http://127.0.0.1:${server.port}/api/ingest/proj-1/`;
    const t = new Transport(url, "pubkey");
    // A large message pushes the body past 4 KiB.
    const big = event("big-1", "x".repeat(6000));
    t.send(big);
    await server.waitFor(1);

    const req = server.requests[0];
    expect(req.gzipped).toBe(true);
    expect(req.headers["content-encoding"]).toBe("gzip");
    // The server decoded it back to the original event.
    expect((req.body as CrashlensEvent).event_id).toBe("big-1");
    expect((req.body as CrashlensEvent).message).toHaveLength(6000);
  });

  it("does not gzip small bodies", async () => {
    const url = `http://127.0.0.1:${server.port}/api/ingest/proj-1/`;
    const t = new Transport(url, "pubkey");
    t.send(event("small-1"));
    await server.waitFor(1);
    expect(server.requests[0].gzipped).toBe(false);
  });
});

describe("Transport - 429 handling", () => {
  it("drops without resending on 429 (Retry-After within cap)", async () => {
    server.setResponder((_req, count) =>
      count === 0
        ? { status: 429, headers: { "Retry-After": "0" } }
        : { status: 202 },
    );
    const url = `http://127.0.0.1:${server.port}/api/ingest/proj-1/`;
    const t = new Transport(url, "pubkey");
    t.send(event("rl-1"));

    // Give the loop time to process and (not) retry.
    await new Promise((r) => setTimeout(r, 200));
    // Exactly one POST: the 429 dropped the event, no resend.
    expect(server.requests).toHaveLength(1);
    expect(t.queueLength()).toBe(0);
  });
});

describe("Transport - 5xx retry", () => {
  it("retries once after a 5xx then drops", async () => {
    server.setResponder((_req, count) =>
      count === 0 ? { status: 500 } : { status: 202 },
    );
    const url = `http://127.0.0.1:${server.port}/api/ingest/proj-1/`;
    const t = new Transport(url, "pubkey");
    t.send(event("retry-1"));

    await server.waitFor(2);
    // Two POSTs of the SAME event id: initial 500 + one retry.
    expect(server.requests).toHaveLength(2);
    const ids = server.requests.map((r) => (r.body as CrashlensEvent).event_id);
    expect(ids).toEqual(["retry-1", "retry-1"]);
    expect(t.queueLength()).toBe(0);
  });
});

describe("Transport - queue overflow", () => {
  it("caps the queue at maxQueue and drops the OLDEST", async () => {
    // Point at a dead port so nothing ever sends; the loop parks on item 0.
    const t = new Transport(
      "http://127.0.0.1:1/api/ingest/proj-1/",
      "pubkey",
      5,
    );
    for (let i = 0; i < 12; i++) t.send(event("e" + i));
    // One item is in-flight (index 0), the rest fill the bounded queue.
    expect(t.queueLength()).toBe(5);
  });
});

describe("Transport - flush", () => {
  it("drains all queued events before the deadline", async () => {
    const url = `http://127.0.0.1:${server.port}/api/ingest/proj-1/`;
    const t = new Transport(url, "pubkey");
    for (let i = 0; i < 5; i++) t.send(event("f" + i));
    const drained = await t.flush(3000);
    expect(drained).toBe(true);
    expect(server.requests).toHaveLength(5);
    expect(t.queueLength()).toBe(0);
  });
});
