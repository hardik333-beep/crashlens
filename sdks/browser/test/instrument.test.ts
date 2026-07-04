import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CrashlensEvent } from "../src/types";

const DSN = "https://pubkey@errors.example.com/api/ingest/proj-1/";

function ok() {
  return { status: 202, headers: { get: () => null } };
}

let sdk: typeof import("../src/index");
let fetchMock: ReturnType<typeof vi.fn>;
const originalFetch = globalThis.fetch;
const originalOnError = window.onerror;
const originalOnRejection = window.onunhandledrejection;

beforeEach(async () => {
  vi.resetModules();
  fetchMock = vi.fn().mockResolvedValue(ok());
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  sdk = await import("../src/index");
});

afterEach(() => {
  sdk.close();
  globalThis.fetch = originalFetch;
  window.onerror = originalOnError;
  window.onunhandledrejection = originalOnRejection;
  vi.restoreAllMocks();
});

async function lastBody(): Promise<CrashlensEvent> {
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
  const calls = fetchMock.mock.calls;
  return JSON.parse((calls[calls.length - 1][1] as RequestInit).body as string);
}

describe("window.onerror wiring", () => {
  it("captures an Error passed to onerror", async () => {
    sdk.init({ dsn: DSN });
    const err = new Error("uncaught boom");
    err.stack =
      "Error: uncaught boom\n    at f (https://app.example.com/app.js:1:1)";
    window.onerror!(
      "uncaught boom",
      "https://app.example.com/app.js",
      1,
      1,
      err,
    );
    const body = await lastBody();
    expect(body.exception?.value).toBe("uncaught boom");
    expect(body.exception?.stacktrace.frames).toHaveLength(1);
  });

  it("builds a synthetic frame when no Error object is available", async () => {
    sdk.init({ dsn: DSN });
    window.onerror!(
      "Script error",
      "https://app.example.com/app.js",
      42,
      7,
      undefined,
    );
    const body = await lastBody();
    expect(body.exception?.value).toBe("Script error");
    const frame = body.exception!.stacktrace.frames[0];
    expect(frame).toMatchObject({
      filename: "https://app.example.com/app.js",
      lineno: 42,
      colno: 7,
    });
  });

  it("chains a previously installed onerror handler", async () => {
    const previous = vi.fn();
    window.onerror = previous;
    sdk.init({ dsn: DSN });
    window.onerror!("boom", "app.js", 1, 1, new Error("boom"));
    await lastBody();
    expect(previous).toHaveBeenCalledTimes(1);
  });
});

describe("window.onunhandledrejection wiring", () => {
  it("captures an Error rejection reason with a stacktrace", async () => {
    sdk.init({ dsn: DSN });
    const reason = new Error("rejected");
    reason.stack =
      "Error: rejected\n    at g (https://app.example.com/app.js:2:2)";
    window.onunhandledrejection!({ reason } as PromiseRejectionEvent);
    const body = await lastBody();
    expect(body.exception?.value).toBe("rejected");
    expect(body.level).toBe("error");
  });

  it("turns a non-Error rejection reason into a message", async () => {
    sdk.init({ dsn: DSN });
    window.onunhandledrejection!({
      reason: "plain string reason",
    } as unknown as PromiseRejectionEvent);
    const body = await lastBody();
    expect(body.exception).toBeUndefined();
    expect(body.message).toContain("plain string reason");
    expect(body.level).toBe("error");
  });
});

describe("navigation breadcrumbs", () => {
  it("records a navigation breadcrumb on history.pushState", async () => {
    sdk.init({ dsn: DSN });
    window.history.pushState({}, "", "/next-page");
    sdk.captureMessage("after nav");
    const body = await lastBody();
    const nav = body.breadcrumbs?.find((b) => b.type === "navigation");
    expect(nav).toBeDefined();
    expect(nav?.data?.to).toContain("/next-page");
  });
});

describe("console breadcrumbs (opt-in)", () => {
  it("records only the first argument of console.error, capped at 200 chars", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    sdk.init({ dsn: DSN, consoleBreadcrumbs: true });
    const longArg = "x".repeat(500);
    console.error(longArg, "second arg ignored");
    sdk.captureMessage("after log");
    const body = await lastBody();
    const crumb = body.breadcrumbs?.find((b) => b.category === "console");
    expect(crumb).toBeDefined();
    expect(crumb?.message).toHaveLength(200);
    expect(crumb?.message).not.toContain("second arg");
  });

  it("does not patch console when the flag is off", async () => {
    sdk.init({ dsn: DSN });
    vi.spyOn(console, "warn").mockImplementation(() => {});
    console.warn("noise");
    sdk.captureMessage("after log");
    const body = await lastBody();
    const crumb = body.breadcrumbs?.find((b) => b.category === "console");
    expect(crumb).toBeUndefined();
  });
});
