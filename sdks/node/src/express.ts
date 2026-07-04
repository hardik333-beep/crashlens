// Express middleware. These are plain functions; express is NOT a dependency
// of this package (there is nothing to import). They are typed against a
// minimal structural shape of the Express request / response / next objects.
//
//   crashlensRequestHandler(): runs each request inside its own async scope so
//     tags and breadcrumbs set while serving one request never bleed into
//     another concurrent request. Mount it BEFORE your routes.
//
//   crashlensErrorHandler(): a 4-arity Express error middleware that captures
//     the error together with the request url and method, then calls next(err)
//     so your own error handling still runs. Mount it AFTER your routes.

import type { Client } from "./client";
import { runInScope } from "./scope";

interface MinimalRequest {
  url?: string;
  originalUrl?: string;
  method?: string;
}

type NextFunction = (err?: unknown) => void;

export type RequestHandler = (
  req: MinimalRequest,
  res: unknown,
  next: NextFunction,
) => void;

export type ErrorRequestHandler = (
  err: unknown,
  req: MinimalRequest,
  res: unknown,
  next: NextFunction,
) => void;

export function makeRequestHandler(): RequestHandler {
  return function crashlensRequest(_req, _res, next): void {
    runInScope(() => next());
  };
}

export function makeErrorHandler(
  getClient: () => Client | null,
): ErrorRequestHandler {
  // Must keep four parameters so Express recognises it as error middleware.
  return function crashlensError(err, req, _res, next): void {
    try {
      const client = getClient();
      if (client) {
        client.captureException(err, "error", {
          url: req.originalUrl ?? req.url,
          method: req.method,
        });
      }
    } catch {
      // an error middleware must never throw
    }
    next(err);
  };
}
