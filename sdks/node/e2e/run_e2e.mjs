// Node SDK end-to-end proof driver (W4-04).
//
// Spawns crasher.mjs, which inits the real built SDK and throws an uncaught
// error, then asserts through the Crashlens issues API that the crash arrived
// and was grouped into an issue with platform "node". Uses only Node built-ins
// plus the global fetch (Node 18+), so it needs nothing beyond `npm run build`.
//
// Environment (set by the CI e2e job):
//   CRASHLENS_DSN, CRASHLENS_API_BASE (e.g. http://localhost/api),
//   CRASHLENS_TOKEN, CRASHLENS_ORG_ID, CRASHLENS_PROJECT_ID.
//
// Exit code 0 on success, 1 on any failure or timeout.

import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { MARKER } from "./marker.mjs";

const EXPECTED_PLATFORM = "node";
const POLL_TIMEOUT_MS = 60_000;

const here = dirname(fileURLToPath(import.meta.url));

function env(name) {
  const value = process.env[name];
  if (!value) {
    throw new Error(`missing required env var ${name}`);
  }
  return value;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function getJson(url, token) {
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    throw new Error(`GET ${url} -> ${res.status}`);
  }
  return res.json();
}

async function runCrasher(dsn) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [join(here, "crasher.mjs")], {
      env: { ...process.env, CRASHLENS_DSN: dsn },
      stdio: "inherit",
    });
    // A non-zero exit is EXPECTED (uncaught exception). We only need it to have
    // flushed the event before exiting.
    child.on("close", (code) => resolve(code));
  });
}

async function main() {
  const dsn = env("CRASHLENS_DSN");
  const apiBase = env("CRASHLENS_API_BASE").replace(/\/$/, "");
  const token = env("CRASHLENS_TOKEN");
  const org = env("CRASHLENS_ORG_ID");
  const project = env("CRASHLENS_PROJECT_ID");

  console.log(
    "running the crasher subprocess (an uncaught exception is expected)...",
  );
  const code = await runCrasher(dsn);
  console.log(`crasher exited with code ${code}`);

  const issuesUrl = `${apiBase}/orgs/${org}/projects/${project}/issues`;
  const deadline = Date.now() + POLL_TIMEOUT_MS;
  let found = null;
  while (Date.now() < deadline) {
    try {
      const listing = await getJson(issuesUrl, token);
      found = (listing.issues ?? []).find((i) =>
        (i.title ?? "").includes(MARKER),
      );
    } catch (err) {
      console.log(`  poll error (retrying): ${err.message}`);
    }
    if (found) {
      break;
    }
    await sleep(1000);
  }

  if (!found) {
    console.error(
      "FAIL: no issue carrying the crasher marker appeared within the timeout",
    );
    process.exit(1);
  }

  const detail = await getJson(`${issuesUrl}/${found.id}`, token);
  const platform = detail?.latest_event?.payload?.platform;
  if (platform !== EXPECTED_PLATFORM) {
    console.error(
      `FAIL: issue platform was ${platform}, expected ${EXPECTED_PLATFORM}`,
    );
    process.exit(1);
  }

  console.log(
    `PASS: uncaught "${found.title}" arrived and grouped with platform=${platform} (issue ${found.id})`,
  );
}

main().catch((err) => {
  console.error(`FAIL: ${err.stack || err.message}`);
  process.exit(1);
});
