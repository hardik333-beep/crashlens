import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { Client } from "../src/client";
import { makeErrorHandler, makeRequestHandler } from "../src/express";
import {
  addBreadcrumb,
  currentScope,
  resetFallback,
  setTag,
  setUser,
} from "../src/scope";
import type { CrashlensEvent, ResolvedOptions } from "../src/types";
import { startServer, type TestServer } from "./helpers";

let server: TestServer;

function opts(over: Partial<ResolvedOptions> = {}): ResolvedOptions {
  return {
    url: `http://127.0.0.1:${server.port}/api/ingest/proj-1/`,
    key: "pubkey",
    environment: "production",
    maxQueue: 100,
    captureUncaughtException: true,
    captureUnhandledRejection: true,
    ...over,
  };
}

beforeEach(async () => {
  server = await startServer();
  resetFallback();
});

afterEach(async () => {
  resetFallback();
  await server.close();
});

describe("Client envelope assembly", () => {
  it("builds a conformant node exception event and sends it", async () => {
    const c = new Client(opts({ release: "web@1.0.0" }));
    setTag("server_name", "web-1");
    setUser({ id: "user-9" });
    addBreadcrumb({ message: "before crash", category: "test" });

    const id = c.captureException(new Error("division by zero"));
    expect(id).toBeDefined();
    await server.waitFor(1);

    const body = server.requests[0].body as CrashlensEvent;
    expect(body.platform).toBe("node");
    expect(body.level).toBe("error");
    expect(body.environment).toBe("production");
    expect(body.release).toBe("web@1.0.0");
    expect(body.sdk).toEqual({ name: "crashlens-node", version: "0.1.0" });
    expect(body.exception?.type).toBe("Error");
    expect(body.exception?.value).toBe("division by zero");
    expect(body.tags).toEqual({ server_name: "web-1" });
    expect(body.user).toEqual({ id: "user-9" });
    expect(body.breadcrumbs).toHaveLength(1);
    expect(body.event_id).toBe(id);
  });

  it("captureMessage sends a log-style event with no exception", async () => {
    const c = new Client(opts());
    c.captureMessage("hello world", "warning");
    await server.waitFor(1);
    const body = server.requests[0].body as CrashlensEvent;
    expect(body.message).toBe("hello world");
    expect(body.level).toBe("warning");
    expect(body.exception).toBeUndefined();
  });

  it("honours beforeSend scrubbing and dropping", async () => {
    const scrub = new Client(
      opts({
        beforeSend: (e) => {
          e.tags = { scrubbed: "true" };
          return e;
        },
      }),
    );
    scrub.captureMessage("scrub me");
    await server.waitFor(1);
    expect((server.requests[0].body as CrashlensEvent).tags).toEqual({
      scrubbed: "true",
    });

    const drop = new Client(opts({ beforeSend: () => null }));
    const id = drop.captureMessage("drop me");
    expect(id).toBeUndefined();
    // Nothing more arrives at the server.
    await new Promise((r) => setTimeout(r, 150));
    expect(server.requests).toHaveLength(1);
  });

  it("applies inAppPathPrefixes to the sent frames", async () => {
    const c = new Client(opts({ inAppPathPrefixes: ["/nonexistent-prefix"] }));
    c.captureException(new Error("prefixed"));
    await server.waitFor(1);
    const body = server.requests[0].body as CrashlensEvent;
    // No real frame starts with the bogus prefix, so all frames are not in_app.
    expect(body.exception?.stacktrace.frames.every((f) => !f.in_app)).toBe(
      true,
    );
  });
});

describe("Express middleware", () => {
  it("crashlensErrorHandler captures with request url + method and calls next", async () => {
    const c = new Client(opts());
    const handler = makeErrorHandler(() => c);
    let forwarded: unknown = undefined;
    const err = new Error("route blew up");
    handler(err, { originalUrl: "/invoices/17", method: "POST" }, {}, (e) => {
      forwarded = e;
    });
    // next(err) must be called with the same error.
    expect(forwarded).toBe(err);

    await server.waitFor(1);
    const body = server.requests[0].body as CrashlensEvent;
    expect(body.request).toEqual({ url: "/invoices/17", method: "POST" });
    expect(body.exception?.value).toBe("route blew up");
  });

  it("crashlensRequestHandler runs the request in an isolated scope", async () => {
    const requestHandler = makeRequestHandler();
    const captured: Record<string, string>[] = [];

    await new Promise<void>((resolve) => {
      requestHandler({}, {}, () => {
        setTag("request", "one");
        captured.push({ ...requireScopeTags() });
        resolve();
      });
    });
    // The per-request tag did not leak into the module fallback.
    expect(captured[0].request).toBe("one");
    // A fresh capture outside the request scope has no such tag.
    const c = new Client(opts());
    c.captureMessage("outside");
    await server.waitFor(1);
    expect((server.requests[0].body as CrashlensEvent).tags).toBeUndefined();
  });
});

// Small helper to read current scope tags.
function requireScopeTags(): Record<string, string> {
  return currentScope().tags;
}
