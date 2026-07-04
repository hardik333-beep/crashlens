// Thin API client. Every call is prefixed with /api (the reverse proxy strips
// that prefix before forwarding to the backend). Authenticated calls attach the
// stored session token as a Bearer header; if the server answers 401 to a call
// we believed was authenticated, the stored token is stale, so we clear it and
// return to the sign-in screen.
import { clearToken, getToken } from "./token";

const API_PREFIX = "/api";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  // Whether to attach the session token. Sign-in, sign-up, and invite
  // acceptance run BEFORE a session exists, so they set this false and handle
  // their own 401 (a bad-credentials message) rather than triggering a redirect.
  authenticated?: boolean;
}

async function extractMessage(response: Response): Promise<string> {
  try {
    const data: unknown = await response.json();
    if (
      data !== null &&
      typeof data === "object" &&
      "detail" in data &&
      typeof (data as { detail: unknown }).detail === "string"
    ) {
      return (data as { detail: string }).detail;
    }
  } catch {
    // Non-JSON error body; fall through to a generic message.
  }
  return `The request failed (status ${response.status}).`;
}

export async function apiRequest<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = "GET", body, authenticated = true } = options;
  const headers: Record<string, string> = {};
  const token = getToken();
  if (authenticated && token !== null) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(`${API_PREFIX}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 401 && authenticated && token !== null) {
    clearToken();
    window.location.assign("/login");
    throw new ApiError(401, "Your session has ended. Please sign in again.");
  }

  if (!response.ok) {
    throw new ApiError(response.status, await extractMessage(response));
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}
