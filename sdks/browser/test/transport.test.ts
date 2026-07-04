import { afterEach, describe, expect, it, vi } from "vitest";
import { Transport } from "../src/transport";
import type { CrashlensEvent } from "../src/types";

function fakeEvent(id: string): CrashlensEvent {
  return {
    event_id: id,
    timestamp: "2026-07-04T12:00:00.000Z",
    platform: "javascript",
    level: "error",
    message: "boom",
    environment: "production",
    sdk: { name: "crashlens-browser", version: "0.1.0" },
  };
}

function ok() {
  return { status: 202, headers: { get: () => null } };
}

function rateLimited(retryAfter: string) {
  return {
    status: 429,
    headers: { get: (k: string) => (k === "Retry-After" ? retryAfter : null) },
  };
}

const URL = "https://errors.example.com/api/ingest/proj-1/";
const KEY = "pubkey";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("Transport - happy path", () => {
  it("POSTs with the ingest URL, key header, and the full envelope", async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok());
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const t = new Transport(URL, KEY);
    t.send(fakeEvent("id-1"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    const [calledUrl, init] = fetchMock.mock.calls[0];
    expect(calledUrl).toBe(URL);
    expect(init.method).toBe("POST");
    expect(init.headers["X-Crashlens-Key"]).toBe(KEY);
    expect(init.headers["Content-Type"]).toContain("application/json");
    expect(init.keepalive).toBe(true);

    const body = JSON.parse(init.body);
    expect(body.event_id).toBe("id-1");
    expect(body.platform).toBe("javascript");
    expect(body.sdk).toEqual({ name: "crashlens-browser", version: "0.1.0" });
    expect(body.environment).toBe("production");
  });

  it("sends queued events sequentially", async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok());
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const t = new Transport(URL, KEY);
    t.send(fakeEvent("a"));
    t.send(fakeEvent("b"));
    t.send(fakeEvent("c"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(t.queueLength()).toBe(0);
  });
});

describe("Transport - queue drop-oldest", () => {
  it("caps the queue at 30 and drops the oldest", async () => {
    // A fetch that never resolves keeps the drain loop parked on the first item.
    const fetchMock = vi.fn().mockReturnValue(new Promise(() => {}));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const t = new Transport(URL, KEY);
    for (let i = 0; i < 35; i++) t.send(fakeEvent("e" + i));

    expect(t.queueLength()).toBe(30);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("Transport - 429 handling", () => {
  it("retries once after Retry-After when within the 5s cap", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(rateLimited("1"))
      .mockResolvedValueOnce(ok());
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const t = new Transport(URL, KEY);
    t.send(fakeEvent("id-1"));

    await vi.advanceTimersByTimeAsync(1500);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(t.queueLength()).toBe(0);
  });

  it("drops without retry when Retry-After exceeds the 5s cap", async () => {
    const fetchMock = vi.fn().mockResolvedValue(rateLimited("10"));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const t = new Transport(URL, KEY);
    t.send(fakeEvent("id-1"));
    await vi.waitFor(() => expect(t.queueLength()).toBe(0));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("Transport - beacon fallback", () => {
  it("uses sendBeacon when fetch is unavailable", async () => {
    // @ts-expect-error - simulate an environment without fetch.
    globalThis.fetch = undefined;
    const beacon = vi.fn().mockReturnValue(true);
    const originalBeacon = navigator.sendBeacon;
    navigator.sendBeacon = beacon;

    const t = new Transport(URL, KEY);
    t.send(fakeEvent("id-1"));
    await vi.waitFor(() => expect(beacon).toHaveBeenCalledTimes(1));
    expect(beacon.mock.calls[0][0]).toBe(URL);

    navigator.sendBeacon = originalBeacon;
  });
});
