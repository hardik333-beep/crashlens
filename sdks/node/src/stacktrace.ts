// Error.stack parsing for Node.js, plus construction of the protocol
// `exception` object (docs/PROTOCOL.md section 3.3).
//
// Node stacks are pure V8 / Chrome format: "    at func (file:line:col)" or
// "    at file:line:col". We therefore need only the V8 branch. V8 reports
// stacks crash-FIRST (the throwing frame is at the top); the protocol requires
// the canonical order (oldest call first, crash frame last), so we reverse
// after parsing.
//
// in_app rules (governor ruling, mirrored from the Python SDK):
//   - When inAppPathPrefixes is provided, a frame is in_app if and only if its
//     filename starts with one of the prefixes. The heuristic is ignored
//     entirely, so an app installed under node_modules (a common container
//     layout) still marks its own frames in_app.
//   - Otherwise, a frame is in_app when it is NOT under node_modules and NOT a
//     node: internal.

import { fileURLToPath } from "node:url";
import type { ExceptionValue, StackFrame } from "./types";

interface RawFrame {
  func: string;
  filename: string;
  lineno: number;
  colno: number;
}

// Pull a trailing url:line:col (or url:line) off a location string.
function splitLocation(location: string): {
  filename: string;
  lineno: number;
  colno: number;
} {
  const withCol = /^(.*?):(\d+):(\d+)$/.exec(location);
  if (withCol) {
    return {
      filename: withCol[1],
      lineno: parseInt(withCol[2], 10),
      colno: parseInt(withCol[3], 10),
    };
  }
  const withLine = /^(.*?):(\d+)$/.exec(location);
  if (withLine) {
    return {
      filename: withLine[1],
      lineno: parseInt(withLine[2], 10),
      colno: 0,
    };
  }
  return { filename: location, lineno: 0, colno: 0 };
}

// file:// URLs (ESM frames report these) become plain filesystem paths so that
// node_modules detection and prefix matching operate on real paths.
function normalizeFilename(filename: string): string {
  if (filename.startsWith("file://")) {
    try {
      return fileURLToPath(filename);
    } catch {
      return filename.replace(/^file:\/\//, "");
    }
  }
  return filename;
}

// Chrome / Edge / V8: "    at func (url:line:col)" or "    at url:line:col".
function parseV8Line(line: string): RawFrame | null {
  const m = /^\s*at\s+(.*)$/.exec(line);
  if (!m) return null;
  const rest = m[1].replace(/^async\s+/, "");

  let func = "";
  let location = rest;
  const paren = /^(.*?)\s*\((.*)\)\s*$/.exec(rest);
  if (paren) {
    func = paren[1];
    location = paren[2];
  }

  // Eval frames: "eval at fn (realUrl:line:col), <anonymous>:1:1". Best effort:
  // point at the real call site captured inside the inner parentheses.
  if (/^eval\b/.test(location)) {
    const inner = /\((\S.*?):(\d+):(\d+)\)/.exec(location);
    if (inner) {
      location = `${inner[1]}:${inner[2]}:${inner[3]}`;
    }
  }

  const loc = splitLocation(location);
  return {
    func: func || "?",
    filename: normalizeFilename(loc.filename),
    lineno: loc.lineno,
    colno: loc.colno,
  };
}

// Parse an Error.stack string into raw frames in the order V8 reported them
// (crash first). Non-frame lines (e.g. the leading "Error: message") are
// skipped.
export function parseStackString(stack: string): RawFrame[] {
  const frames: RawFrame[] = [];
  for (const line of stack.split("\n")) {
    if (!line.trim()) continue;
    if (!/^\s*at\s/.test(line)) continue;
    const frame = parseV8Line(line);
    if (frame) frames.push(frame);
  }
  return frames;
}

function isInApp(filename: string, prefixes: string[] | undefined): boolean {
  if (prefixes && prefixes.length > 0) {
    // Prefix match on the path DECIDES ALONE.
    return prefixes.some((p) => filename.startsWith(p));
  }
  if (filename.startsWith("node:")) return false;
  if (filename.includes("node_modules")) return false;
  return true;
}

// Build protocol StackFrames from a stack string: parsed, reversed to
// crash-last, with in_app computed per the rules above.
export function stackFramesFromString(
  stack: string,
  prefixes?: string[],
): StackFrame[] {
  return parseStackString(stack)
    .reverse()
    .map((f) => ({
      filename: f.filename,
      function: f.func,
      lineno: f.lineno,
      colno: f.colno,
      in_app: isInApp(f.filename, prefixes),
    }));
}

// Maximum exception chain depth per docs/PROTOCOL.md ruling 6 (root + up to 4
// causes = 5 total). Deeper chains are dropped client-side.
const MAX_CAUSE_DEPTH = 5;

// Build the protocol `exception` object from a thrown value. Non-Error throws
// (strings, objects) still produce a valid exception with an empty stacktrace.
export function exceptionFromError(
  err: unknown,
  prefixes?: string[],
  depth = 1,
): ExceptionValue {
  const asError = err as {
    name?: unknown;
    message?: unknown;
    stack?: unknown;
    cause?: unknown;
  } | null;

  const type =
    asError && typeof asError.name === "string" && asError.name
      ? asError.name
      : "Error";
  const value =
    asError && typeof asError.message === "string"
      ? asError.message
      : safeToString(err);
  const stack =
    asError && typeof asError.stack === "string" ? asError.stack : "";

  const exception: ExceptionValue = {
    type,
    value,
    stacktrace: { frames: stackFramesFromString(stack, prefixes) },
  };

  const cause = asError ? asError.cause : undefined;
  if (cause !== undefined && cause !== null && depth < MAX_CAUSE_DEPTH) {
    exception.cause = exceptionFromError(cause, prefixes, depth + 1);
  }

  return exception;
}

function safeToString(value: unknown): string {
  try {
    return String(value);
  } catch {
    return "unknown error";
  }
}
