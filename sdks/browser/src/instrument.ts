// Automatic capture and breadcrumb instrumentation. installInstrumentation
// wires the global handlers and returns a cleanup function that restores every
// original it replaced (so close() fully unwinds the SDK).

import type { Client } from "./client";
import type { ResolvedOptions } from "./types";
import { guard, stringifyArg } from "./util";

type HistoryFn = (
  this: History,
  data: unknown,
  unused: string,
  url?: string | URL | null,
) => void;

export function installInstrumentation(
  client: Client,
  options: ResolvedOptions,
): () => void {
  const restorers: Array<() => void> = [];

  if (typeof window !== "undefined") {
    installOnError(client, restorers);
    installOnUnhandledRejection(client, restorers);
    installHistory(client, restorers);
    installLifecycle(client, restorers);
  }
  if (options.consoleBreadcrumbs && typeof console !== "undefined") {
    installConsole(client, restorers);
  }

  return () => {
    for (const restore of restorers) guard(restore);
  };
}

function installOnError(client: Client, restorers: Array<() => void>): void {
  const previous = window.onerror;
  window.onerror = function (
    this: Window,
    message: Event | string,
    source?: string,
    lineno?: number,
    colno?: number,
    error?: Error,
  ): boolean {
    guard(() =>
      client.captureOnError(
        typeof message === "string" ? message : (message as ErrorEvent).message,
        source,
        lineno,
        colno,
        error,
      ),
    );
    if (typeof previous === "function") {
      return Boolean(
        previous.call(this, message, source, lineno, colno, error),
      );
    }
    return false;
  };
  restorers.push(() => {
    window.onerror = previous;
  });
}

function installOnUnhandledRejection(
  client: Client,
  restorers: Array<() => void>,
): void {
  const previous = window.onunhandledrejection;
  window.onunhandledrejection = function (
    this: WindowEventHandlers,
    event: PromiseRejectionEvent,
  ): void {
    guard(() => {
      const reason = event ? event.reason : undefined;
      if (reason instanceof Error) {
        client.captureException(reason, "error");
      } else {
        client.captureMessage(
          "Unhandled promise rejection: " + stringifyArg(reason, 8000),
          "error",
        );
      }
    });
    if (typeof previous === "function") {
      previous.call(window, event);
    }
  };
  restorers.push(() => {
    window.onunhandledrejection = previous;
  });
}

function installHistory(client: Client, restorers: Array<() => void>): void {
  const hist = window.history;
  if (!hist) return;

  const wrap = (name: "pushState" | "replaceState"): void => {
    const original = hist[name] as HistoryFn;
    if (typeof original !== "function") return;
    const patched: HistoryFn = function (this: History, data, unused, url) {
      guard(() => {
        const from = currentPath();
        const to = url != null ? String(url) : from;
        client.addBreadcrumb({
          type: "navigation",
          category: "navigation",
          message: from + " -> " + to,
          data: { from, to },
        });
      });
      return original.call(this, data, unused, url);
    };
    hist[name] = patched as History[typeof name];
    restorers.push(() => {
      hist[name] = original as History[typeof name];
    });
  };

  wrap("pushState");
  wrap("replaceState");

  const onPopState = (): void => {
    guard(() =>
      client.addBreadcrumb({
        type: "navigation",
        category: "navigation",
        message: "popstate -> " + currentPath(),
        data: { to: currentPath() },
      }),
    );
  };
  window.addEventListener("popstate", onPopState);
  restorers.push(() => window.removeEventListener("popstate", onPopState));
}

function installLifecycle(client: Client, restorers: Array<() => void>): void {
  const onHide = (): void => guard(() => client.flush());
  const onVisibility = (): void => {
    if (
      typeof document !== "undefined" &&
      document.visibilityState === "hidden"
    ) {
      guard(() => client.flush());
    }
  };
  window.addEventListener("pagehide", onHide);
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", onVisibility);
  }
  restorers.push(() => {
    window.removeEventListener("pagehide", onHide);
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", onVisibility);
    }
  });
}

function installConsole(client: Client, restorers: Array<() => void>): void {
  for (const method of ["warn", "error"] as const) {
    const original = console[method];
    if (typeof original !== "function") continue;
    console[method] = function (this: Console, ...args: unknown[]): void {
      guard(() =>
        client.addBreadcrumb({
          type: "default",
          category: "console",
          level: method === "warn" ? "warning" : "error",
          // Record ONLY the first argument, stringified to 200 chars.
          message: args.length > 0 ? stringifyArg(args[0], 200) : "",
        }),
      );
      original.apply(this, args);
    };
    restorers.push(() => {
      console[method] = original;
    });
  }
}

function currentPath(): string {
  if (typeof location === "undefined" || !location) return "";
  return location.pathname + location.search + location.hash;
}
