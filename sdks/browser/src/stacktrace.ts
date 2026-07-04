// Error.stack parsing for the two browser engine families, plus construction of
// the protocol `exception` object (docs/PROTOCOL.md section 3.3).
//
// Browsers report stacks crash-FIRST (the throwing frame is at the top). The
// protocol requires the canonical order: oldest call first, crash frame last.
// We therefore reverse after parsing.
//
// in_app is true for every frame at v1. Source maps and real in_app inference
// land in a later slice. FLAG: in_app default is under governor review.

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

// Chrome / Edge / V8: "    at func (url:line:col)" or "    at url:line:col".
function parseChromeLine(line: string): RawFrame | null {
  const m = /^\s*at\s+(.*)$/.exec(line);
  if (!m) return null;
  let rest = m[1].replace(/^async\s+/, "");

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
  return { func: func || "?", ...loc };
}

// Firefox / Safari: "func@url:line:col", "@url:line:col", "func@[native code]".
function parseFirefoxLine(line: string): RawFrame | null {
  const m = /^\s*([^@]*)@(.*)$/.exec(line);
  if (!m) return null;
  const func = m[1] || "?";
  const loc = splitLocation(m[2]);
  return { func, ...loc };
}

// Parse an Error.stack string into raw frames in the order the engine reported
// them (crash first). Non-frame lines (e.g. the leading "Error: message") are
// skipped.
export function parseStackString(stack: string): RawFrame[] {
  const frames: RawFrame[] = [];
  for (const line of stack.split("\n")) {
    if (!line.trim()) continue;
    let frame: RawFrame | null = null;
    if (/^\s*at\s/.test(line)) {
      frame = parseChromeLine(line);
    } else if (line.indexOf("@") !== -1) {
      frame = parseFirefoxLine(line);
    }
    if (frame) frames.push(frame);
  }
  return frames;
}

function toStackFrames(stack: string): StackFrame[] {
  // Reverse: engines report crash-first; the protocol wants oldest-first,
  // crash-last.
  return parseStackString(stack)
    .reverse()
    .map((f) => ({
      filename: f.filename,
      function: f.func,
      lineno: f.lineno,
      colno: f.colno,
      in_app: true,
    }));
}

// Maximum exception chain depth per docs/PROTOCOL.md ruling 6 (root + up to 4
// causes = 5 total). Deeper chains are dropped client-side.
const MAX_CAUSE_DEPTH = 5;

// Build the protocol `exception` object from a thrown value. Non-Error throws
// (strings, objects) still produce a valid exception with an empty stacktrace.
export function exceptionFromError(err: unknown, depth = 1): ExceptionValue {
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
    stacktrace: { frames: toStackFrames(stack) },
  };

  const cause = asError ? asError.cause : undefined;
  if (cause !== undefined && cause !== null && depth < MAX_CAUSE_DEPTH) {
    exception.cause = exceptionFromError(cause, depth + 1);
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
