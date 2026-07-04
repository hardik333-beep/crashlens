// Shared test helpers: a real local ingest server, and an esbuild bundling step
// so child-process fixtures can import the SDK as a single .mjs file.

import { build } from "esbuild";
import { createServer, type IncomingMessage, type Server } from "node:http";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { gunzipSync } from "node:zlib";

export interface ReceivedRequest {
  method: string;
  url: string;
  headers: Record<string, string | string[] | undefined>;
  gzipped: boolean;
  body: unknown;
}

export interface TestServer {
  port: number;
  dsn: (projectId?: string) => string;
  requests: ReceivedRequest[];
  // Resolve once at least `n` requests have arrived (or reject after timeoutMs).
  waitFor: (n: number, timeoutMs?: number) => Promise<void>;
  setResponder: (
    fn: (req: IncomingMessage, count: number) => Responder,
  ) => void;
  close: () => Promise<void>;
}

export interface Responder {
  status: number;
  headers?: Record<string, string>;
  body?: string;
}

function readBody(req: IncomingMessage): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

export async function startServer(): Promise<TestServer> {
  const requests: ReceivedRequest[] = [];
  let responder: (req: IncomingMessage, count: number) => Responder = () => ({
    status: 202,
  });
  let count = 0;

  const server: Server = createServer(async (req, res) => {
    const raw = await readBody(req);
    const gzipped = req.headers["content-encoding"] === "gzip";
    let body: unknown = null;
    try {
      const text = (gzipped ? gunzipSync(raw) : raw).toString("utf-8");
      body = text ? JSON.parse(text) : null;
    } catch {
      body = null;
    }
    requests.push({
      method: req.method ?? "",
      url: req.url ?? "",
      headers: req.headers,
      gzipped,
      body,
    });
    const out = responder(req, count);
    count += 1;
    const echoId =
      body && typeof body === "object" && body !== null
        ? (body as { event_id?: string }).event_id
        : undefined;
    const responseBody = out.body ?? JSON.stringify({ id: echoId ?? null });
    res.writeHead(out.status, {
      "Content-Type": "application/json",
      ...(out.headers ?? {}),
    });
    res.end(responseBody);
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;

  return {
    port,
    requests,
    dsn: (projectId = "proj-1") =>
      `http://testkey@127.0.0.1:${port}/api/ingest/${projectId}/`,
    waitFor: (n, timeoutMs = 5000) =>
      new Promise<void>((resolve, reject) => {
        const started = Date.now();
        const tick = (): void => {
          if (requests.length >= n) return resolve();
          if (Date.now() - started > timeoutMs) {
            return reject(
              new Error(
                `timed out waiting for ${n} request(s); got ${requests.length}`,
              ),
            );
          }
          setTimeout(tick, 10);
        };
        tick();
      }),
    setResponder: (fn) => {
      responder = fn;
    },
    close: () =>
      new Promise<void>((resolve, reject) =>
        server.close((err) => (err ? reject(err) : resolve())),
      ),
  };
}

// Bundle the SDK to a temp .mjs file so a spawned node process can import it.
let cachedBundle: string | null = null;
export async function bundleSdk(): Promise<string> {
  if (cachedBundle) return cachedBundle;
  const dir = mkdtempSync(join(tmpdir(), "crashlens-node-"));
  const outfile = join(dir, "sdk.mjs");
  await build({
    entryPoints: [fileURLToPath(new URL("../src/index.ts", import.meta.url))],
    bundle: true,
    format: "esm",
    platform: "node",
    target: ["node18"],
    outfile,
  });
  cachedBundle = outfile;
  return outfile;
}
