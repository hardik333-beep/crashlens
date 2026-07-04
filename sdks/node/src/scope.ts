// Per-context scope (tags, breadcrumbs, user) backed by AsyncLocalStorage.
//
// Concurrent requests must not bleed context into each other. Each request that
// runs inside `runInScope` gets its own Scope forked from the current one, so a
// tag or breadcrumb set while serving request A never appears on request B.
//
// Outside any ALS run (for example a module-level setTag at startup, or a plain
// script with no Express request handler) mutations land on a module-level
// fallback scope. When a request scope is forked it copies the fallback, so
// process-wide tags set before the request still apply, while later per-request
// writes stay isolated.

import { AsyncLocalStorage } from "node:async_hooks";
import { BreadcrumbBuffer } from "./breadcrumbs";
import type { Breadcrumb, BreadcrumbInput, Level } from "./types";
import { nowIso } from "./util";

// The protocol keeps the newest 100 breadcrumbs per event, so we never buffer
// more than that client-side.
export const MAX_BREADCRUMBS = 100;

export class Scope {
  tags: Record<string, string> = {};
  user: { id?: string } | undefined = undefined;
  breadcrumbs: BreadcrumbBuffer;

  constructor(maxBreadcrumbs: number = MAX_BREADCRUMBS) {
    this.breadcrumbs = new BreadcrumbBuffer(maxBreadcrumbs);
  }

  clone(): Scope {
    const copy = new Scope(this.breadcrumbs.max);
    copy.tags = { ...this.tags };
    copy.user = this.user ? { ...this.user } : undefined;
    copy.breadcrumbs = this.breadcrumbs.clone();
    return copy;
  }
}

const als = new AsyncLocalStorage<Scope>();
let fallback = new Scope();

// The scope any mutation or capture applies to: the ALS store when inside a
// run, otherwise the module-level fallback.
export function currentScope(): Scope {
  return als.getStore() ?? fallback;
}

// Run `fn` inside a fresh scope forked from the current one. Downstream async
// work started by `fn` inherits this scope.
export function runInScope<T>(fn: () => T): T {
  return als.run(currentScope().clone(), fn);
}

export function setTag(key: string, value: string): void {
  currentScope().tags[String(key)] = String(value);
}

export function setUser(user: { id?: string } | null): void {
  currentScope().user = user === null ? undefined : user;
}

export function addBreadcrumb(input: BreadcrumbInput): void {
  const crumb: Breadcrumb = {
    timestamp: input.timestamp || nowIso(),
    type: input.type,
    category: input.category,
    level: input.level as Level | undefined,
    message: input.message,
    data: input.data,
  };
  currentScope().breadcrumbs.add(crumb);
}

// Test-only: reset the fallback scope to a clean state.
export function resetFallback(): void {
  fallback = new Scope();
}
