// The core client. Holds SDK state (transport, breadcrumbs, tags, user) and
// assembles protocol events (docs/PROTOCOL.md section 3).

import { BreadcrumbBuffer } from "./breadcrumbs";
import { exceptionFromError } from "./stacktrace";
import { Transport } from "./transport";
import type {
  Breadcrumb,
  BreadcrumbInput,
  CrashlensEvent,
  ExceptionValue,
  Level,
  ResolvedOptions,
} from "./types";
import { nowIso, uuid4 } from "./util";

export const SDK_NAME = "crashlens-browser";
export const SDK_VERSION = "0.1.0";

export class Client {
  private readonly transport: Transport;
  private readonly breadcrumbs: BreadcrumbBuffer;
  private readonly options: ResolvedOptions;
  private tags: Record<string, string> = {};
  private user: { id?: string } | undefined;

  constructor(options: ResolvedOptions) {
    this.options = options;
    this.transport = new Transport(options.url, options.key);
    this.breadcrumbs = new BreadcrumbBuffer(options.maxBreadcrumbs);
  }

  captureException(error: unknown, level: Level = "error"): string | undefined {
    const event = this.buildEvent({
      level,
      exception: exceptionFromError(error),
    });
    return this.dispatch(event);
  }

  // Used by the window.onerror handler when no Error object is available: build
  // a single synthetic frame from the browser-supplied location.
  captureOnError(
    message: unknown,
    source: string | undefined,
    lineno: number | undefined,
    colno: number | undefined,
    error: unknown,
  ): string | undefined {
    if (error instanceof Error) {
      return this.captureException(error, "error");
    }
    const exception: ExceptionValue = {
      type: "Error",
      value: typeof message === "string" ? message : String(message),
      stacktrace: {
        frames: [
          {
            filename: source || currentUrl() || "",
            function: "?",
            lineno: lineno || 0,
            colno: colno || 0,
            in_app: true,
          },
        ],
      },
    };
    return this.dispatch(this.buildEvent({ level: "error", exception }));
  }

  captureMessage(message: string, level: Level = "info"): string | undefined {
    return this.dispatch(this.buildEvent({ level, message: String(message) }));
  }

  addBreadcrumb(input: BreadcrumbInput): void {
    const crumb: Breadcrumb = {
      timestamp: input.timestamp || nowIso(),
      type: input.type,
      category: input.category,
      level: input.level,
      message: input.message,
      data: input.data,
    };
    this.breadcrumbs.add(crumb);
  }

  setTag(key: string, value: string): void {
    this.tags[String(key)] = String(value);
  }

  setUser(user: { id?: string } | null): void {
    this.user = user === null ? undefined : user;
  }

  flush(): void {
    this.transport.flush();
  }

  close(): void {
    this.transport.close();
    this.breadcrumbs.clear();
  }

  private buildEvent(partial: {
    level: Level;
    message?: string;
    exception?: ExceptionValue;
  }): CrashlensEvent {
    const crumbs = this.breadcrumbs.all();
    const event: CrashlensEvent = {
      event_id: uuid4(),
      timestamp: nowIso(),
      platform: "javascript",
      level: partial.level,
      environment: this.options.environment,
      sdk: { name: SDK_NAME, version: SDK_VERSION },
    };
    if (partial.message !== undefined) event.message = partial.message;
    if (partial.exception !== undefined) event.exception = partial.exception;
    if (crumbs.length > 0) event.breadcrumbs = crumbs;
    if (Object.keys(this.tags).length > 0) event.tags = { ...this.tags };
    if (this.options.release !== undefined)
      event.release = this.options.release;
    if (this.user !== undefined) event.user = this.user;
    const url = currentUrl();
    if (url !== undefined) event.request = { url };
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

function currentUrl(): string | undefined {
  return typeof location !== "undefined" && location
    ? location.href
    : undefined;
}
