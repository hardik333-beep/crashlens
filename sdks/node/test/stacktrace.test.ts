import { describe, expect, it } from "vitest";
import {
  exceptionFromError,
  parseStackString,
  stackFramesFromString,
} from "../src/stacktrace";

// A representative V8 (Node) stack: crash-first, mixing a named frame, an
// anonymous frame, an async frame, a node_modules frame, an ESM file:// frame,
// and a node: internal frame.
const V8_STACK = [
  "Error: boom",
  "    at compute (/app/billing/invoice.js:42:12)",
  "    at Object.<anonymous> (/app/index.js:10:1)",
  "    at async handler (/app/routes/pay.js:7:5)",
  "    at middleware (/app/node_modules/express/lib/router.js:100:3)",
  "    at file:///app/esm/loader.mjs:3:7",
  "    at process.processTicksAndRejections (node:internal/process/task_queues:96:5)",
].join("\n");

describe("parseStackString (V8 only)", () => {
  it("parses named, anonymous, async, node_modules, file:// and node: frames", () => {
    const frames = parseStackString(V8_STACK);
    expect(frames).toHaveLength(6);

    expect(frames[0]).toEqual({
      func: "compute",
      filename: "/app/billing/invoice.js",
      lineno: 42,
      colno: 12,
    });
    expect(frames[1].func).toBe("Object.<anonymous>");
    // async prefix is stripped from the function.
    expect(frames[2].func).toBe("handler");
    expect(frames[3].filename).toContain("node_modules");
    // file:// is normalised to a plain path.
    expect(frames[4].filename).toBe("/app/esm/loader.mjs");
    expect(frames[4].filename).not.toContain("file://");
    expect(frames[5].filename).toBe("node:internal/process/task_queues");
  });

  it("skips the leading error message line", () => {
    const frames = parseStackString(V8_STACK);
    expect(frames.every((f) => f.filename !== "Error: boom")).toBe(true);
  });
});

describe("stackFramesFromString ordering + in_app heuristic", () => {
  it("reverses to crash-last (oldest call first)", () => {
    const frames = stackFramesFromString(V8_STACK);
    // Last frame is the crash site (compute); first is the deepest node: frame.
    expect(frames[frames.length - 1].function).toBe("compute");
    expect(frames[0].filename).toBe("node:internal/process/task_queues");
  });

  it("marks app frames in_app and library / internal frames not in_app", () => {
    const byFile: Record<string, boolean> = {};
    for (const f of stackFramesFromString(V8_STACK)) {
      byFile[f.filename] = f.in_app;
    }
    expect(byFile["/app/billing/invoice.js"]).toBe(true);
    expect(byFile["/app/index.js"]).toBe(true);
    expect(byFile["/app/esm/loader.mjs"]).toBe(true);
    expect(byFile["/app/node_modules/express/lib/router.js"]).toBe(false);
    expect(byFile["node:internal/process/task_queues"]).toBe(false);
  });
});

describe("stackFramesFromString inAppPathPrefixes override", () => {
  it("lets a prefix decide alone, overriding the node_modules heuristic", () => {
    const frames = stackFramesFromString(V8_STACK, ["/app"]);
    const byFile: Record<string, boolean> = {};
    for (const f of frames) byFile[f.filename] = f.in_app;
    // Everything under /app is in_app, including the node_modules frame.
    expect(byFile["/app/node_modules/express/lib/router.js"]).toBe(true);
    expect(byFile["/app/billing/invoice.js"]).toBe(true);
    // The node: internal frame is not under the prefix, so it is not in_app.
    expect(byFile["node:internal/process/task_queues"]).toBe(false);
  });

  it("marks a non-matching app frame as not in_app when a prefix is set", () => {
    const stack = ["Error: x", "    at f (/srv/other/thing.js:1:1)"].join("\n");
    const frames = stackFramesFromString(stack, ["/app"]);
    expect(frames[0].in_app).toBe(false);
  });
});

describe("exceptionFromError", () => {
  it("builds a protocol exception from a real Error with crash-last frames", () => {
    const err = new Error("kaboom");
    const exc = exceptionFromError(err);
    expect(exc.type).toBe("Error");
    expect(exc.value).toBe("kaboom");
    expect(Array.isArray(exc.stacktrace.frames)).toBe(true);
    // The throwing frame (this test function) is the last frame.
    const last = exc.stacktrace.frames[exc.stacktrace.frames.length - 1];
    expect(last.filename).toContain("stacktrace.test");
  });

  it("captures the Error.cause chain and caps it at depth 5", () => {
    let err: Error & { cause?: unknown } = new Error("root");
    for (let i = 0; i < 8; i++) {
      const next = new Error(`level-${i}`) as Error & { cause?: unknown };
      next.cause = err;
      err = next;
    }
    const exc = exceptionFromError(err);
    // Count the chain depth by following .cause.
    let depth = 1;
    let node = exc;
    while (node.cause) {
      depth += 1;
      node = node.cause;
    }
    expect(depth).toBe(5);
  });

  it("handles a non-Error throw with an empty stacktrace", () => {
    const exc = exceptionFromError("just a string");
    expect(exc.type).toBe("Error");
    expect(exc.value).toBe("just a string");
    expect(exc.stacktrace.frames).toEqual([]);
  });

  it("uses a custom error name as the type", () => {
    class ValidationError extends Error {}
    const e = new ValidationError("bad input");
    e.name = "ValidationError";
    expect(exceptionFromError(e).type).toBe("ValidationError");
  });
});
