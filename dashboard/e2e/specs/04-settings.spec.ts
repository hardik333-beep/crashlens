// Flow (g): organization settings. Add a webhook alert channel (pointed at a
// dummy https URL that never fires during the test) and show the activity /
// audit section that records sensitive actions.

import { expect, test } from "@playwright/test";

import { readSharedAccount, screenshotPath } from "../fixtures/shared";

test("add a webhook alert and view the activity log", async ({ page }) => {
  const account = readSharedAccount();

  await page.goto(`/org/${account.orgId}/settings`);
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Alerts" })).toBeVisible();

  // Choose the webhook alert type (the only select that has a webhook option),
  // which reveals the URL field.
  const typeSelect = page
    .locator("select")
    .filter({ has: page.locator('option[value="webhook"]') });
  await typeSelect.selectOption("webhook");

  await page
    .locator('input[type="url"]')
    .fill("https://example.com/hooks/crashlens");
  await page.getByRole("button", { name: /^Add alert$/ }).click();

  // The new channel appears as a card in the channel list. Assert on the LIST
  // ROW (li.card with its p.card-title type label), never on bare page text:
  // "Send to a webhook" also exists as an <option> inside the collapsed type
  // select above, and Playwright treats options in a closed select as hidden,
  // so a page-wide text match can resolve to a never-visible node. The row also
  // shows the masked destination (scheme + host only, see alerts.mask_target).
  const channelCard = page.locator("li.card").filter({
    has: page.locator("p.card-title", { hasText: "Send to a webhook" }),
  });
  await expect(channelCard).toBeVisible();
  await expect(channelCard).toContainText("https://example.com");

  // The activity section records the sensitive actions taken so far (project,
  // key, invite and channel creation all write audit rows).
  await expect(page.getByRole("heading", { name: "Activity" })).toBeVisible();

  await page.screenshot({
    path: screenshotPath("09-settings-alerts"),
    fullPage: true,
  });
});
