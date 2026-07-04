// Small dependency-free helpers (node builtins only).

import { randomUUID } from "node:crypto";

// RFC4122 v4 UUID from the platform crypto (always present in Node 18+).
export function uuid4(): string {
  return randomUUID();
}

// RFC3339 UTC timestamp, e.g. "2026-07-04T12:00:00.000Z".
export function nowIso(): string {
  return new Date().toISOString();
}

// Run a function, swallowing any error so the SDK never throws into the host
// application (a Tier-1 guarantee of this SDK).
export function guard<T>(fn: () => T): T | undefined {
  try {
    return fn();
  } catch {
    return undefined;
  }
}

export function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
