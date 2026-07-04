// Automatic capture: process-level uncaughtException and unhandledRejection
// handlers, plus a beforeExit flush hook.
//
// uncaughtException: we capture the error (level fatal), best-effort flush, and
// then honestly mirror Node's default process semantics. Node's default, when
// NO uncaughtException listener is registered, is to print the error and exit
// with code 1. By registering a listener we suppress that default, so we
// reproduce it ourselves: if ours is the ONLY listener we print the error and
// exit(1) after the flush; if other listeners exist we do nothing extra (we do
// not exit) and leave the process fate to them, matching what a user who added
// their own handler would expect.
//
// unhandledRejection: we capture (level error) and NEVER alter process
// behaviour. Node's own default for unhandled rejections is unchanged.
//
// Both are opt-out via init flags (captureUncaughtException /
// captureUnhandledRejection).

import type { Client } from "./client";
import type { ResolvedOptions } from "./types";

export function installInstrumentation(
  client: Client,
  options: ResolvedOptions,
): () => void {
  const registered: Array<[string, (...args: never[]) => void]> = [];

  if (options.captureUncaughtException) {
    const onUncaught = (err: unknown): void => {
      try {
        client.captureException(err, "fatal");
      } catch {
        // capture must not throw during a crash
      }
      const others = process
        .listeners("uncaughtException")
        .filter((l) => l !== (onUncaught as unknown));
      const soleHandler = others.length === 0;
      const finish = (): void => {
        if (soleHandler) {
          // Mirror Node's default termination for an unhandled exception.
          try {
            process.stderr.write(
              (err instanceof Error ? err.stack : String(err)) + "\n",
            );
          } catch {
            // ignore a failed stderr write during shutdown
          }
          process.exit(1);
        }
      };
      client.flush(2000).then(finish, finish);
    };
    process.on("uncaughtException", onUncaught);
    registered.push([
      "uncaughtException",
      onUncaught as (...a: never[]) => void,
    ]);
  }

  if (options.captureUnhandledRejection) {
    const onRejection = (reason: unknown): void => {
      try {
        client.captureException(reason, "error");
      } catch {
        // never interfere with the host's own rejection handling
      }
    };
    process.on("unhandledRejection", onRejection);
    registered.push([
      "unhandledRejection",
      onRejection as (...a: never[]) => void,
    ]);
  }

  // Flush any buffered events when the event loop is about to empty.
  const onBeforeExit = (): void => {
    void client.flush(2000);
  };
  process.on("beforeExit", onBeforeExit);
  registered.push(["beforeExit", onBeforeExit as (...a: never[]) => void]);

  return () => {
    for (const [event, handler] of registered) {
      process.removeListener(event, handler as (...args: unknown[]) => void);
    }
  };
}
