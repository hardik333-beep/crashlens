import { describe, expect, it } from "vitest";
import { exceptionFromError, parseStackString } from "../src/stacktrace";

// Representative real-world stack strings. Engines report crash-FIRST; the SDK
// reverses to the protocol's oldest-first, crash-last order.

const CHROME_STACK = [
  "TypeError: Cannot read properties of undefined (reading 'total')",
  "    at multiply (https://app.example.com/static/app.js:10:17)",
  "    at https://app.example.com/static/app.js:22:5",
  "    at Object.<anonymous> (https://app.example.com/static/app.js:30:3)",
  "    at eval (eval at run (https://app.example.com/static/app.js:40:2), <anonymous>:1:1)",
].join("\n");

const FIREFOX_STACK = [
  "multiply@https://app.example.com/static/app.js:10:17",
  "@https://app.example.com/static/app.js:22:5",
  "run/<@https://app.example.com/static/app.js:30:3",
  "dispatch@https://app.example.com/static/app.js:40:2",
].join("\n");

describe("parseStackString - Chrome/V8 format", () => {
  const frames = parseStackString(CHROME_STACK);

  it("parses every frame and skips the header line", () => {
    expect(frames).toHaveLength(4);
  });

  it("parses a named frame with url:line:col", () => {
    expect(frames[0]).toMatchObject({
      func: "multiply",
      filename: "https://app.example.com/static/app.js",
      lineno: 10,
      colno: 17,
    });
  });

  it("marks an anonymous frame with no function name as ?", () => {
    expect(frames[1]).toMatchObject({ func: "?", lineno: 22, colno: 5 });
  });

  it("keeps dotted/member function names", () => {
    expect(frames[2].func).toBe("Object.<anonymous>");
  });

  it("resolves an eval frame to its inner call site (best effort)", () => {
    expect(frames[3]).toMatchObject({
      func: "eval",
      filename: "https://app.example.com/static/app.js",
      lineno: 40,
      colno: 2,
    });
  });
});

describe("parseStackString - Firefox/Safari format", () => {
  const frames = parseStackString(FIREFOX_STACK);

  it("parses every frame", () => {
    expect(frames).toHaveLength(4);
  });

  it("parses a named frame", () => {
    expect(frames[0]).toMatchObject({
      func: "multiply",
      lineno: 10,
      colno: 17,
    });
  });

  it("treats a leading @ (no name) as an anonymous frame", () => {
    expect(frames[1]).toMatchObject({ func: "?", lineno: 22, colno: 5 });
  });

  it("keeps closure-style function names", () => {
    expect(frames[2].func).toBe("run/<");
  });
});

describe("exceptionFromError - frame order and shape", () => {
  it("reverses Chrome frames to oldest-first, crash-last", () => {
    const err = new Error("boom");
    err.stack = CHROME_STACK;
    const exc = exceptionFromError(err);
    const frames = exc.stacktrace.frames;
    // Crash frame (multiply) must be LAST after normalisation.
    expect(frames[frames.length - 1].function).toBe("multiply");
    // Oldest call (the eval frame) must be FIRST.
    expect(frames[0].function).toBe("eval");
    expect(frames.every((f) => f.in_app === true)).toBe(true);
  });

  it("carries type and value from the Error", () => {
    const err = new TypeError("bad thing");
    err.stack = CHROME_STACK;
    const exc = exceptionFromError(err);
    expect(exc.type).toBe("TypeError");
    expect(exc.value).toBe("bad thing");
  });

  it("handles non-Error throws with an empty stacktrace", () => {
    const exc = exceptionFromError("just a string");
    expect(exc.type).toBe("Error");
    expect(exc.value).toBe("just a string");
    expect(exc.stacktrace.frames).toEqual([]);
  });
});

describe("exceptionFromError - cause chain", () => {
  it("caps the chain at 5 exceptions (root + 4 causes)", () => {
    // Build a chain of 8 nested causes.
    let err: Error = new Error("root");
    for (let i = 0; i < 7; i++) {
      const next = new Error("level " + i);
      (next as Error & { cause?: unknown }).cause = err;
      err = next;
    }
    const exc = exceptionFromError(err);
    let depth = 1;
    let cursor = exc;
    while (cursor.cause) {
      cursor = cursor.cause;
      depth++;
    }
    expect(depth).toBe(5);
  });

  it("links a single cause", () => {
    const root = new Error("db offline");
    const wrapper = new Error("request failed");
    (wrapper as Error & { cause?: unknown }).cause = root;
    const exc = exceptionFromError(wrapper);
    expect(exc.value).toBe("request failed");
    expect(exc.cause?.value).toBe("db offline");
  });
});
