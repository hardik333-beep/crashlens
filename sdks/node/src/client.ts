// The core client. Holds SDK state (transport, resolved options) and assembles
// protocol events (docs/PROTOCOL.md section 3). Tags, breadcrumbs, and user are
// read from the current async scope (see scope.ts) so concurrent requests do
// not share context.
//
// Every public method is exception-safe: the SDK never throws into the host
// application.

import {
  addBreadcrumb as scopeAddBreadcrumb,
  currentScope,
  setTag as scopeSetTag,
  setUser as scopeSetUser,
} from "./scope";
import { exceptionFromError } from "./stacktrace";
import { Transport } from "./transport";
import type {
  BreadcrumbInput,
  CrashlensEvent,
  ExceptionValue,
  Level,
  RequestData,
  ResolvedOptions,
} from "./types";
import { nowIso, uuid4 } from "./util";

export const SDK_NAME = "crashlens-node";
export const SDK_VERSION = "0.1.0";

export class Client {
  private readonly transport: Transport;
  private readonly options: ResolvedOptions;

  constructor(options: ResolvedOptions) {
    this.options = options;
    this.transport = new Transport(options.url, options.key, options.maxQueue);
  }

  captureException(
    error: unknown,
    level: Level = "error",
    request?: RequestData,
  ): string | undefined {
    try {
      const event = this.buildEvent({
        level,
        exception: exceptionFromError(error, this.options.inAppPathPrefixes),
        request,
      });
      return this.dispatch(event);
    } catch {
      return undefined;
    }
  }

  captureMessage(
    message: string,
    level: Level = "info",
    request?: RequestData,
  ): string | undefined {
    try {
      return this.dispatch(
        this.buildEvent({ level, message: String(message), request }),
      );
    } catch {
      return undefined;
    }
  }

  addBreadcrumb(input: BreadcrumbInput): void {
    scopeAddBreadcrumb(input);
  }

  setTag(key: string, value: string): void {
    scopeSetTag(key, value);
  }

  setUser(user: { id?: string } | null): void {
    scopeSetUser(user);
  }

  flush(timeoutMs = 5000): Promise<boolean> {
    return this.transport.flush(timeoutMs);
  }

  async close(timeoutMs = 5000): Promise<void> {
    await this.transport.flush(timeoutMs);
    this.transport.close();
  }

  private buildEvent(partial: {
    level: Level;
    message?: string;
    exception?: ExceptionValue;
    request?: RequestData;
  }): CrashlensEvent {
    const scope = currentScope();
    const crumbs = scope.breadcrumbs.all();
    const event: CrashlensEvent = {
      event_id: uuid4(),
      timestamp: nowIso(),
      platform: "node",
      level: partial.level,
      environment: this.options.environment,
      sdk: { name: SDK_NAME, version: SDK_VERSION },
    };
    if (partial.message !== undefined) event.message = partial.message;
    if (partial.exception !== undefined) event.exception = partial.exception;
    if (crumbs.length > 0) event.breadcrumbs = crumbs;
    if (Object.keys(scope.tags).length > 0) event.tags = { ...scope.tags };
    if (this.options.release !== undefined)
      event.release = this.options.release;
    if (scope.user !== undefined) event.user = scope.user;
    if (partial.request) {
      const request: RequestData = {};
      if (partial.request.url !== undefined) request.url = partial.request.url;
      if (partial.request.method !== undefined)
        request.method = partial.request.method;
      if (Object.keys(request).length > 0) event.request = request;
    }
    return event;
  }

  private dispatch(event: CrashlensEvent): string | undefined {
    let finalEvent: CrashlensEvent | null = event;
    if (this.options.beforeSend) {
      try {
        finalEvent = this.options.beforeSend(event);
      } catch {
        finalEvent = event;
      }
    }
    if (!finalEvent) return undefined; // dropped by beforeSend
    this.transport.send(finalEvent);
    return finalEvent.event_id;
  }
}
