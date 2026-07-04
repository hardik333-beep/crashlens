// Shared constants and helpers for the Crashlens e2e suite.
//
// Nothing here talks to the browser; it is pure filesystem + value helpers used
// by the setup project (which signs an account up through the API and stashes
// the result) and by every spec (which reads that account back and writes its
// numbered screenshots).

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

// The exact localStorage key the dashboard reads its session token from. Kept
// character-for-character in sync with dashboard/src/lib/token.ts: the whole
// point of the API-token login pattern is that we write the token under the SAME
// key the app reads, so RequireAuth sees a logged-in user with no UI form.
export const TOKEN_STORAGE_KEY = "crashlens.session.token";

// A password that clears the server policy (>= 10 chars and not in the common
// list, see server/app/security.py). Not a secret: it only ever exists inside a
// throwaway CI database.
export const STRONG_PASSWORD = "Crashlens-E2E-Verify-8842";

// Where the setup project stashes the created account for the specs to read.
const AUTH_DIR = join(process.cwd(), "e2e", ".auth");
export const STORAGE_STATE_PATH = join(AUTH_DIR, "state.json");
const CONTEXT_PATH = join(AUTH_DIR, "context.json");

// Where every spec writes its numbered product-tour screenshot. CI copies these
// into docs/images/ and commits them.
export const SCREENSHOT_DIR = join(process.cwd(), "e2e", "screenshots");

// The account the setup project created and every spec logs in as.
export interface SharedAccount {
  token: string;
  userId: string;
  orgId: string;
  email: string;
}

export function ensureDir(dir: string): void {
  if (!existsSync(dir)) {
    mkdirSync(dir, { recursive: true });
  }
}

export function writeSharedAccount(account: SharedAccount): void {
  ensureDir(AUTH_DIR);
  writeFileSync(CONTEXT_PATH, JSON.stringify(account, null, 2), "utf-8");
}

export function readSharedAccount(): SharedAccount {
  const raw = readFileSync(CONTEXT_PATH, "utf-8");
  return JSON.parse(raw) as SharedAccount;
}

// A Playwright storageState document that injects the token into localStorage
// for the http://localhost origin, exactly as the app expects to find it.
export function tokenStorageState(token: string): {
  cookies: never[];
  origins: {
    origin: string;
    localStorage: { name: string; value: string }[];
  }[];
} {
  return {
    cookies: [],
    origins: [
      {
        origin: "http://localhost",
        localStorage: [{ name: TOKEN_STORAGE_KEY, value: token }],
      },
    ],
  };
}

// A path under SCREENSHOT_DIR for a numbered tour frame, e.g.
// screenshotPath("05-issues-list") -> .../e2e/screenshots/05-issues-list.png.
export function screenshotPath(name: string): string {
  ensureDir(SCREENSHOT_DIR);
  return join(SCREENSHOT_DIR, `${name}.png`);
}

// A per-run unique email so re-runs against a persistent database never collide
// on the unique email constraint.
export function uniqueEmail(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.com`;
}
