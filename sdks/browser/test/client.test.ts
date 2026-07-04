import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CrashlensEvent } from "../src/types";

const DSN = "https://pubkey@errors.example.com/api/ingest/proj-1/";

function ok() {
  return { status: 202, headers: { get: () => null } };
}

// Each test gets a fresh copy of the singleton module so init state and the
// one-time warning flag do not leak between tests.
let sdk: typeof import("../src/index");
let fetchMock: ReturnType<typeof vi.fn>;
const originalFetch = globalThis.fetch;

beforeEach(async () => {
  vi.resetModules();
  fetchMock = vi.fn().mockResolvedValue(ok());
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  sdk = await import("../src/index");
});

afterEach(() => {
  sdk.close();
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

async function sentBodies(): Promise<CrashlensEvent[]> {
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
  return fetchMock.mock.calls.map((c) =>
    JSON.parse((c[1] as RequestInit).body as string),
  );
}

describe("init and manual capture", () => {
  it("captureMessage sends a message event with required envelope fields", async () => {
    sdk.init({ dsn: DSN });
    const id = sdk.captureMessage("hello world", "warning");
    const [body] = await sentBodies();
    expect(id).toBe(body.event_id);
    expect(body.message).toBe("hello world");
    expect(body.level).toBe("warning");
    expect(body.platform).toBe("javascript");
    expect(body.environment).toBe("production");
    expect(body.timestamp).toMatch(/^\d{4}-\d{2}-\d{2}T.*Z$/);
    expect(body.sdk.name).toBe("crashlens-browser");
    expect(body.request?.url).toBeDefined();
  });

  it("captureException sends a reversed, crash-last stacktrace", async () => {
    sdk.init({ dsn: DSN });
    const err = new Error("kaboom");
    err.stack = [
      "Error: kaboom",
      "    at inner (https://app.example.com/app.js:5:1)",
      "    at outer (https://app.example.com/app.js:9:1)",
    ].join("\n");
    sdk.captureException(err);
    const [body] = await sentBodies();
    const frames = body.exception!.stacktrace.frames;
    expect(frames[frames.length - 1].function).toBe("inner");
    expect(frames[0].function).toBe("outer");
  });

  it("applies environment and release from init", async () => {
    sdk.init({ dsn: DSN, environment: "staging", release: "web@2.0.0" });
    sdk.captureMessage("x");
    const [body] = await sentBodies();
    expect(body.environment).toBe("staging");
    expect(body.release).toBe("web@2.0.0");
  });

  it("accepts url + key instead of a DSN", async () => {
    sdk.init({
      url: "https://errors.example.com/api/ingest/proj-1/",
      key: "pubkey",
    });
    sdk.captureMessage("x");
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock.mock.calls[0][0]).toBe(
      "https://errors.example.com/api/ingest/proj-1/",
    );
    expect(
      (fetchMock.mock.calls[0][1] as RequestInit).headers as Record<
        string,
        string
      >,
    ).toMatchObject({ "X-Crashlens-Key": "pubkey" });
  });

  it("is idempotent: a second init does not reset state", async () => {
    sdk.init({ dsn: DSN, environment: "staging" });
    sdk.init({ dsn: DSN, environment: "production" }); // ignored
    sdk.captureMessage("x");
    const [body] = await sentBodies();
    expect(body.environment).toBe("staging");
  });
});

describe("tags, user, breadcrumbs", () => {
  it("attaches tags and user", async () => {
    sdk.init({ dsn: DSN });
    sdk.setTag("feature", "checkout");
    sdk.setUser({ id: "user-42" });
    sdk.captureMessage("x");
    const [body] = await sentBodies();
    expect(body.tags).toMatchObject({ feature: "checkout" });
    expect(body.user).toEqual({ id: "user-42" });
  });

  it("attaches breadcrumbs newest last", async () => {
    sdk.init({ dsn: DSN });
    sdk.addBreadcrumb({ message: "first", category: "test" });
    sdk.addBreadcrumb({ message: "second", category: "test" });
    sdk.captureMessage("x");
    const [body] = await sentBodies();
    expect(body.breadcrumbs?.map((b) => b.message)).toEqual([
      "first",
      "second",
    ]);
    expect(body.breadcrumbs?.[0].timestamp).toBeDefined();
  });
});

describe("beforeSend", () => {
  it("drops the event when beforeSend returns null", async () => {
    sdk.init({ dsn: DSN, beforeSend: () => null });
    const id = sdk.captureMessage("secret");
    expect(id).toBeUndefined();
    // Give any async send a chance to (not) fire.
    await new Promise((r) => setTimeout(r, 10));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("scrubs fields when beforeSend mutates the event", async () => {
    sdk.init({
      dsn: DSN,
      beforeSend: (event) => {
        event.message = "[redacted]";
        return event;
      },
    });
    sdk.captureMessage("sensitive value");
    const [body] = await sentBodies();
    expect(body.message).toBe("[redacted]");
  });
});

describe("misconfiguration", () => {
  it("warns once and disables reporting when no dsn/url is given", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    sdk.init({});
    sdk.captureMessage("x");
    await new Promise((r) => setTimeout(r, 10));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledTimes(1);
  });
});
