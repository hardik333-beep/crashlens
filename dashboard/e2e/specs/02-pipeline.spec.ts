// Flows (b), (i), (c), (d), (e) as one serial pipeline, because they share the
// project and key created at the top:
//
//   b. create a project through the UI, then create a DSN key on its detail page
//   i. show the sampling control and source maps section on that same page
//   c. seed real errors THROUGH THE PUBLIC INGEST PIPELINE with the key, then
//      poll the issues API until the background worker has grouped them (this is
//      also the protocol-level transport proof)
//   d. the issues list with the seeded issues, filter tabs, and search
//   e. an issue's detail (stack trace, breadcrumbs, tags, activity chart), the
//      resolve action changing its status, and a comment
//
// Runs serially in one worker so the shared project/key/issue flow through the
// steps in order.

import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { APIRequestContext, expect, test } from "@playwright/test";

import {
  readSharedAccount,
  screenshotPath,
  type SharedAccount,
} from "../fixtures/shared";

let account: SharedAccount;
let projectId = "";
let publicKey = "";

// A realistic seed envelope with its volatile fields (event_id, timestamp)
// freshened per send so a re-run never dedupes against a previous one.
interface SeedEnvelope {
  event_id: string;
  timestamp: string;
  [key: string]: unknown;
}

function loadSeedEnvelopes(): SeedEnvelope[] {
  const raw = readFileSync(
    join(process.cwd(), "e2e", "fixtures", "seed-events.json"),
    "utf-8",
  );
  return JSON.parse(raw) as SeedEnvelope[];
}

async function issueTotal(
  request: APIRequestContext,
  orgId: string,
  token: string,
): Promise<number> {
  const res = await request.get(
    `/api/orgs/${orgId}/projects/${projectId}/issues`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  expect(res.status(), await res.text()).toBe(200);
  const body = (await res.json()) as { total: number };
  return body.total;
}

test.describe.serial("errors from ingest to triage", () => {
  test.beforeAll(() => {
    account = readSharedAccount();
  });

  test("create a project", async ({ page }) => {
    await page.goto(`/org/${account.orgId}/projects`);
    await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();

    await page.getByPlaceholder("Payments API").fill("Payments API");
    await page.getByPlaceholder("python").fill("python");
    await page.getByRole("button", { name: /^Add project$/ }).click();

    // The new project appears as a card whose title links into its issues view.
    const card = page.getByRole("link", { name: "Payments API" });
    await expect(card).toBeVisible();
    await page.screenshot({
      path: screenshotPath("02-projects"),
      fullPage: true,
    });

    // Follow the card into the issues view to learn the project id from the URL.
    await card.click();
    await page.waitForURL(/\/org\/[^/]+\/projects\/[^/]+\/issues$/);
    const match = /\/projects\/([^/]+)\/issues$/.exec(page.url());
    expect(match).not.toBeNull();
    projectId = match![1];
  });

  test("create a DSN key and show it on the project detail page", async ({
    page,
  }) => {
    await page.goto(`/org/${account.orgId}/projects/${projectId}`);
    await expect(
      page.getByRole("heading", { name: "Payments API" }),
    ).toBeVisible();

    await page.getByRole("button", { name: /^Create key$/ }).click();

    // The generated public key renders in a <code class="mono"> row with a
    // "Copy key" control next to it.
    const keyCode = page.locator("code.mono").first();
    await expect(keyCode).toBeVisible();
    publicKey = (await keyCode.innerText()).trim();
    expect(publicKey.length).toBeGreaterThan(10);
    await expect(
      page.getByRole("button", { name: "Copy key" }).first(),
    ).toBeVisible();

    await page.screenshot({
      path: screenshotPath("03-project-key"),
      fullPage: true,
    });
  });

  test("show the sampling control and source maps section", async ({
    page,
  }) => {
    await page.goto(`/org/${account.orgId}/projects/${projectId}`);
    const sampling = page.getByRole("heading", {
      name: "Keep every event, or keep a sample of events",
    });
    await expect(sampling).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Source maps" }),
    ).toBeVisible();
    // Bring the lower settings into view, then capture the whole page so both
    // the sampling control and the source maps section are visible.
    await page
      .getByRole("heading", { name: "Source maps" })
      .scrollIntoViewIfNeeded();
    await page.screenshot({
      path: screenshotPath("04-project-settings"),
      fullPage: true,
    });
  });

  test("seed real errors through the public ingest pipeline", async ({
    request,
  }) => {
    const envelopes = loadSeedEnvelopes();
    for (const template of envelopes) {
      const envelope: SeedEnvelope = {
        ...template,
        event_id: randomUUID(),
        timestamp: new Date().toISOString(),
      };
      const res = await request.post(`/api/ingest/${projectId}/`, {
        headers: { "X-Crashlens-Key": publicKey },
        data: envelope,
      });
      // 202 Accepted is the documented success acknowledgement (docs/PROTOCOL.md).
      expect(res.status(), await res.text()).toBe(202);
      const ack = (await res.json()) as { id: string | null };
      expect(ack.id).toBe(envelope.event_id);
    }

    // Poll the issues API until the worker has grouped all four distinct errors.
    const deadline = Date.now() + 60_000;
    let total = 0;
    while (Date.now() < deadline) {
      total = await issueTotal(request, account.orgId, account.token);
      if (total >= envelopes.length) {
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    expect(
      total,
      "the worker should have grouped the four seeded errors into four issues",
    ).toBeGreaterThanOrEqual(envelopes.length);
  });

  test("browse the issues list, filter and search", async ({ page }) => {
    await page.goto(`/org/${account.orgId}/projects/${projectId}/issues`);
    await expect(page.getByRole("heading", { name: "Errors" })).toBeVisible();
    // All four seeded issues are open, so the default (Open) tab shows them.
    await expect(page.locator("a.issue-title")).toHaveCount(4);
    await page.screenshot({
      path: screenshotPath("05-issues-list"),
      fullPage: true,
    });

    // Filter tabs: the Fixed tab is empty (nothing resolved yet), All shows all.
    await page.getByRole("tab", { name: "Fixed" }).click();
    await expect(page.locator(".empty-title")).toBeVisible();
    await page.getByRole("tab", { name: "All" }).click();
    await expect(page.locator("a.issue-title").first()).toBeVisible();

    // Search narrows to the matching title.
    await page.getByRole("tab", { name: "Open" }).click();
    await page.getByPlaceholder("Search error titles").fill("TypeError");
    await page.getByRole("button", { name: "Search" }).click();
    await expect(page.locator("a.issue-title")).toHaveCount(1);
    await expect(page.locator("a.issue-title")).toContainText("TypeError");
  });

  test("open an issue, resolve it, and comment", async ({ page }) => {
    // The TypeError issue carries breadcrumbs, tags and a release, so its detail
    // page is the richest for the walkthrough screenshot.
    await page.goto(`/org/${account.orgId}/projects/${projectId}/issues`);
    const typeErrorLink = page
      .locator("a.issue-title")
      .filter({ hasText: "TypeError" })
      .first();
    await expect(typeErrorLink).toBeVisible();
    await typeErrorLink.click();
    await page.waitForURL(/\/issues\/[^/]+$/);

    // Stack trace, activity chart, tags and breadcrumbs are all on the page.
    await expect(
      page.getByRole("heading", { name: "What went wrong" }),
    ).toBeVisible();
    await expect(page.getByText("Activity")).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Leading up to it" }),
    ).toBeVisible();
    await expect(page.locator(".tag-chip").first()).toBeVisible();
    await page.screenshot({
      path: screenshotPath("06-issue-detail"),
      fullPage: true,
    });

    // Resolve (the "Mark as fixed" action) and confirm the status flips to Fixed.
    await page.getByRole("button", { name: "Mark as fixed" }).click();
    await expect(page.locator(".status-badge")).toContainText("Fixed");
    await page.screenshot({
      path: screenshotPath("07-issue-resolved"),
      fullPage: true,
    });

    // Add a comment and confirm it renders.
    await page
      .getByPlaceholder("Add notes for your team about this error...")
      .fill("Root cause: a null amount reached the currency formatter.");
    await page.getByRole("button", { name: /^Add comment$/ }).click();
    await expect(
      page.getByText(
        "Root cause: a null amount reached the currency formatter.",
      ),
    ).toBeVisible();
  });
});
