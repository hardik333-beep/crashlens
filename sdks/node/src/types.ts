// Type definitions for the Crashlens v1 event envelope (docs/PROTOCOL.md section 3).
// These describe the exact JSON shape the SDK sends to a Crashlens instance.
// This SDK reports the "node" platform.

export type Level = "fatal" | "error" | "warning" | "info" | "debug";

export interface StackFrame {
  filename: string;
  function: string;
  lineno: number;
  colno: number;
  in_app: boolean;
}

export interface ExceptionValue {
  type: string;
  value: string;
  stacktrace: { frames: StackFrame[] };
  cause?: ExceptionValue;
}

export interface Breadcrumb {
  timestamp: string;
  type?: string;
  category?: string;
  level?: Level;
  message?: string;
  data?: Record<string, unknown>;
}

export interface RequestData {
  url?: string;
  method?: string;
}

export interface CrashlensEvent {
  event_id: string;
  timestamp: string;
  platform: "node";
  level: Level;
  message?: string;
  exception?: ExceptionValue;
  breadcrumbs?: Breadcrumb[];
  tags?: Record<string, string>;
  environment: string;
  release?: string;
  sdk: { name: string; version: string };
  user?: { id?: string };
  request?: RequestData;
}

// A breadcrumb as supplied by a caller: timestamp and defaults are filled in
// by the SDK.
export type BreadcrumbInput = Omit<Breadcrumb, "timestamp"> & {
  timestamp?: string;
};

export interface InitOptions {
  // A DSN of the form http(s)://<public_key>@<host>[:port]/api/ingest/<project_id>/
  dsn?: string;
  // Alternatively, the full ingest URL and the public key supplied separately.
  url?: string;
  key?: string;
  environment?: string;
  release?: string;
  // Path prefixes that mark application code. When provided, a frame is in_app
  // if and only if its filename starts with one of these prefixes; the
  // node_modules / node: heuristic is ignored entirely (governor ruling,
  // mirrored from the Python SDK).
  inAppPathPrefixes?: string[];
  // Maximum number of events buffered in the transport queue before the oldest
  // is dropped.
  maxQueue?: number;
  // A hook to scrub or drop an event before it is sent. Return null to drop.
  beforeSend?: (event: CrashlensEvent) => CrashlensEvent | null;
  // Automatic capture toggles. Both default to true.
  captureUncaughtException?: boolean;
  captureUnhandledRejection?: boolean;
}

// Fully resolved options after DSN parsing and default application.
export interface ResolvedOptions {
  url: string;
  key: string;
  environment: string;
  release?: string;
  inAppPathPrefixes?: string[];
  maxQueue: number;
  beforeSend?: (event: CrashlensEvent) => CrashlensEvent | null;
  captureUncaughtException: boolean;
  captureUnhandledRejection: boolean;
}
