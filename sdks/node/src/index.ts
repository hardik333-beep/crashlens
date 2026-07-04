// Public entry point for @crashlens/node.
//
// One import plus a single init() line and uncaught Node errors appear in the
// Crashlens dashboard. Every public function is wrapped so the SDK never throws
// into the host application and never writes console noise (a single one-time
// warning if init is misconfigured).

import { Client } from "./client";
import { parseDsn } from "./dsn";
import {
  makeErrorHandler,
  makeRequestHandler,
  type ErrorRequestHandler,
  type RequestHandler,
} from "./express";
import { installInstrumentation } from "./instrument";
import type {
  BreadcrumbInput,
  InitOptions,
  Level,
  ResolvedOptions,
} from "./types";
import { guard } from "./util";

let client: Client | null = null;
let uninstall: (() => void) | null = null;
let warned = false;

function warnOnce(message: string): void {
  if (warned) return;
  warned = true;
  if (typeof console !== "undefined" && typeof console.warn === "function") {
    console.warn(message);
  }
}

function resolveOptions(options: InitOptions): ResolvedOptions | null {
  let url: string | undefined;
  let key: string | undefined;

  if (options.dsn) {
    try {
      const parsed = parseDsn(options.dsn);
      url = parsed.url;
      key = parsed.key;
    } catch {
      warnOnce("Crashlens: invalid DSN; error reporting is disabled.");
      return null;
    }
  } else if (options.url && options.key) {
    url = options.url;
    key = options.key;
  }

  if (!url || !key) {
    warnOnce(
      "Crashlens: init requires a dsn (or url + key); error reporting is disabled.",
    );
    return null;
  }

  return {
    url,
    key,
    environment: options.environment ?? "production",
    release: options.release,
    inAppPathPrefixes: options.inAppPathPrefixes,
    maxQueue: options.maxQueue ?? 100,
    beforeSend: options.beforeSend,
    captureUncaughtException: options.captureUncaughtException ?? true,
    captureUnhandledRejection: options.captureUnhandledRejection ?? true,
  };
}

// Initialise the SDK. Idempotent: a second call while already initialised is a
// no-op (call close() first to re-init with new options).
export function init(options: InitOptions): void {
  guard(() => {
    if (client) return;
    const resolved = resolveOptions(options);
    if (!resolved) return;
    client = new Client(resolved);
    uninstall = installInstrumentation(client, resolved);
  });
}

export function captureException(error: unknown): string | undefined {
  return guard(() => client?.captureException(error));
}

export function captureMessage(
  message: string,
  level: Level = "info",
): string | undefined {
  return guard(() => client?.captureMessage(message, level));
}

export function addBreadcrumb(breadcrumb: BreadcrumbInput): void {
  guard(() => client?.addBreadcrumb(breadcrumb));
}

export function setTag(key: string, value: string): void {
  guard(() => client?.setTag(key, value));
}

export function setUser(user: { id?: string } | null): void {
  guard(() => client?.setUser(user));
}

// Drain buffered events, resolving true if the queue emptied before the
// deadline. Safe to await before a graceful shutdown.
export function flush(timeoutMs = 5000): Promise<boolean> {
  const active = client;
  if (!active) return Promise.resolve(true);
  return active.flush(timeoutMs).catch(() => false);
}

// Flush, remove the process instrumentation, and tear down the client.
export async function close(timeoutMs = 5000): Promise<void> {
  const active = client;
  const teardown = uninstall;
  client = null;
  uninstall = null;
  try {
    teardown?.();
    if (active) await active.close(timeoutMs);
  } catch {
    // close must never throw
  }
}

// Express error middleware (4-arity). Mount AFTER your routes.
export function crashlensErrorHandler(): ErrorRequestHandler {
  return makeErrorHandler(() => client);
}

// Express request middleware. Mount BEFORE your routes so each request runs in
// its own async scope.
export function crashlensRequestHandler(): RequestHandler {
  return makeRequestHandler();
}

export type {
  Breadcrumb,
  BreadcrumbInput,
  CrashlensEvent,
  ExceptionValue,
  InitOptions,
  Level,
  RequestData,
  StackFrame,
} from "./types";
export type { ErrorRequestHandler, RequestHandler } from "./express";
