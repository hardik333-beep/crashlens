// Public entry point for @crashlens/browser.
//
// A tiny script/import plus one init line and uncaught browser errors appear in
// the Crashlens dashboard. Every public function is wrapped so the SDK never
// throws into the host page and never writes console noise (a single one-time
// warning if init is misconfigured).

import { Client } from "./client";
import { parseDsn } from "./dsn";
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
    maxBreadcrumbs: options.maxBreadcrumbs ?? 100,
    consoleBreadcrumbs: options.consoleBreadcrumbs ?? false,
    beforeSend: options.beforeSend,
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

export function close(): void {
  guard(() => {
    uninstall?.();
    client?.close();
    client = null;
    uninstall = null;
  });
}

export type {
  Breadcrumb,
  BreadcrumbInput,
  CrashlensEvent,
  ExceptionValue,
  InitOptions,
  Level,
  StackFrame,
} from "./types";
