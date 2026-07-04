import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import type { CrashlensEvent } from "../src/types";
import { bundleSdk, startServer, type TestServer } from "./helpers";

let sdkPath: string;
let server: TestServer;

interface RunResult {
  code: number | null;
  stderr: string;
}

function runFixture(name: string, dsn: string): Promise<RunResult> {
  const fixture = fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url));
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [fixture], {
      env: { ...process.env, SDK_PATH: sdkPath, DSN: dsn },
      stdio: ["ignore", "ignore", "pipe"],
    });
    let stderr = "";
    child.stderr.on("data", (c: Buffer) => {
      stderr += c.toString();
    });
    child.on("error", reject);
    child.on("close", (code) => resolve({ code, stderr }));
  });
}

beforeAll(async () => {
  sdkPath = await bundleSdk();
}, 30000);

beforeEach(async () => {
  server = await startServer();
});

afterEach(async () => {
  await server.close();
});

describe("uncaughtException wiring", () => {
  it("captures the error, flushes, and exits 1 when it is the sole handler", async () => {
    const result = await runFixture("uncaught-sole.mjs", server.dsn());
    await server.waitFor(1);

    const body = server.requests[0].body as CrashlensEvent;
    expect(body.level).toBe("fatal");
    expect(body.exception?.value).toBe("boom-uncaught-sole");
    // Mirrors Node's default: sole handler prints and exits 1.
    expect(result.code).toBe(1);
    expect(result.stderr).toContain("boom-uncaught-sole");
  });

  it("captures but does NOT force exit(1) when another handler exists", async () => {
    const result = await runFixture("uncaught-withhandler.mjs", server.dsn());
    await server.waitFor(1);

    const body = server.requests[0].body as CrashlensEvent;
    expect(body.exception?.value).toBe("boom-uncaught-withhandler");
    // The user's handler chose exit code 7; our SDK did not override it.
    expect(result.code).toBe(7);
  });
});

describe("unhandledRejection wiring", () => {
  it("captures the rejection without altering process behaviour (exit 0)", async () => {
    const result = await runFixture("unhandled.mjs", server.dsn());
    await server.waitFor(1);

    const body = server.requests[0].body as CrashlensEvent;
    expect(body.level).toBe("error");
    expect(body.exception?.value).toBe("boom-unhandled-rejection");
    // Process controlled its own exit; the SDK did not force a code.
    expect(result.code).toBe(0);
  });
});
