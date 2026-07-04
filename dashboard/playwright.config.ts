// Playwright configuration for the Crashlens end-to-end click suite.
//
// The suite runs against the FULL self-hosted stack brought up by
// docker-compose (caddy on :80 in front of the API, worker, Postgres and
// Redis). It never runs against the Vite dev server, so there is no webServer
// block here: CI stands the stack up first, then invokes `playwright test`.
//
// baseURL is http://localhost because caddy serves both the compiled dashboard
// and the reverse-proxied /api on port 80. A page loaded from this origin can
// therefore POST to /api/ingest same-origin, with no CORS preflight (the API
// ships no CORS middleware on purpose - see server/app/main.py).
//
// One chromium project, a 1440x900 viewport, screenshots captured on failure,
// and a single retry. Tests run with a single worker and in file order so the
// numbered product-tour screenshots are produced deterministically and the
// shared instance-admin account is never raced.

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  // Deterministic order + no cross-test races on the shared account.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 1,
  // A generous per-test timeout: several tests seed events through the public
  // ingest pipeline and then poll the issues API until the background worker
  // has grouped them.
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: "playwright-report" }],
  ],
  use: {
    baseURL: process.env.CRASHLENS_BASE_URL ?? "http://localhost",
    viewport: { width: 1440, height: 900 },
    screenshot: "on",
    trace: "on-first-retry",
    video: "off",
  },
  projects: [
    {
      name: "setup",
      testMatch: /setup\/auth\.setup\.ts$/,
    },
    {
      name: "chromium",
      testMatch: /specs\/.*\.spec\.ts$/,
      dependencies: ["setup"],
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
        // Every spec starts already authenticated as the instance-admin account
        // the setup project created (token injected into localStorage under the
        // exact key the app reads).
        storageState: "e2e/.auth/state.json",
      },
    },
  ],
});
