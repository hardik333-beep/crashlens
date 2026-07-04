// W4-04 browser SDK end-to-end proof (also a Playwright flow).
//
// Loads a MINIMAL page served same-origin as the stack (via a fulfilled route on
// http://localhost, so the SDK's fetch to /api/ingest is same-origin with no
// CORS), imports the BUILT iife bundle, inits it with a real DSN, and throws an
// uncaught error. The browser SDK's window.onerror handler captures and sends it
// through the public ingest pipeline. We then assert, via the issues API, that
// the error arrived and was grouped with platform "javascript", and screenshot
// the resulting issue detail (which shows the original browser frames).

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { APIRequestContext, expect, test } from "@playwright/test";

import {
  readSharedAccount,
  screenshotPath,
  type SharedAccount,
} from "../fixtures/shared";

const MARKER = "crashlens browser e2e uncaught marker";

async function createProjectAndKey(
  request: APIRequestContext,
  account: SharedAccount,
): Promise<{ projectId: string; publicKey: string }> {
  const auth = { Authorization: `Bearer ${account.token}` };
  const projectRes = await request.post(`/api/orgs/${account.orgId}/projects`, {
    headers: auth,
    data: { name: "Browser demo", platform: "javascript" },
  });
  expect(projectRes.status(), await projectRes.text()).toBe(201);
  const projectId = ((await projectRes.json()) as { id: string }).id;

  const keyRes = await request.post(
    `/api/orgs/${account.orgId}/projects/${projectId}/keys`,
    { headers: auth },
  );
  expect(keyRes.status(), await keyRes.text()).toBe(201);
  const publicKey = ((await keyRes.json()) as { public_key: string })
    .public_key;
  return { projectId, publicKey };
}

// Poll the issues API until an issue whose latest event has platform
// "javascript" appears, returning its id. Throws on timeout.
async function findJavascriptIssue(
  request: APIRequestContext,
  account: SharedAccount,
  projectId: string,
): Promise<string> {
  const auth = { Authorization: `Bearer ${account.token}` };
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    const listRes = await request.get(
      `/api/orgs/${account.orgId}/projects/${projectId}/issues`,
      { headers: auth },
    );
    if (listRes.ok()) {
      const list = (await listRes.json()) as { issues: { id: string }[] };
      for (const issue of list.issues) {
        const detailRes = await request.get(
          `/api/orgs/${account.orgId}/projects/${projectId}/issues/${issue.id}`,
          { headers: auth },
        );
        if (detailRes.ok()) {
          const detail = (await detailRes.json()) as {
            latest_event: { payload: { platform?: string } } | null;
          };
          if (detail.latest_event?.payload?.platform === "javascript") {
            return issue.id;
          }
        }
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error("timed out waiting for a javascript-platform issue");
}

test("browser SDK captures an uncaught error end to end", async ({
  page,
  request,
}) => {
  const account = readSharedAccount();
  const { projectId, publicKey } = await createProjectAndKey(request, account);

  const iife = readFileSync(
    join(process.cwd(), "..", "sdks", "browser", "dist", "crashlens.iife.js"),
    "utf-8",
  );
  const dsn = `http://${publicKey}@localhost/api/ingest/${projectId}/`;

  // A minimal same-origin page: the built SDK bundle, then init + an uncaught
  // throw on a later tick so the window.onerror handler is installed first.
  const html = `<!doctype html><html><head><meta charset="utf-8"><title>Browser SDK e2e</title></head><body>
<h1>Crashlens browser SDK end-to-end</h1>
<script>${iife}</script>
<script>
  Crashlens.init({ dsn: ${JSON.stringify(dsn)}, release: "web@3.0.0" });
  setTimeout(function () { throw new Error(${JSON.stringify(MARKER)}); }, 50);
</script>
</body></html>`;

  const pageUrl = "http://localhost/__crashlens_browser_e2e__";
  await page.route(pageUrl, (route) =>
    route.fulfill({ contentType: "text/html; charset=utf-8", body: html }),
  );
  await page.goto(pageUrl);
  // Give the SDK a moment to hand the event to its transport queue.
  await page.waitForTimeout(1000);

  const issueId = await findJavascriptIssue(request, account, projectId);

  // Show the captured browser error in the dashboard.
  await page.goto(
    `/org/${account.orgId}/projects/${projectId}/issues/${issueId}`,
  );
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Error");
  await page.screenshot({
    path: screenshotPath("12-browser-sdk-issue"),
    fullPage: true,
  });
});
