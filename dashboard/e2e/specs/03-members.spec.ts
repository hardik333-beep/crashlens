// Flow (f): the team members page and creating an invite, screenshotting the
// one-time invite link. Uses a throwaway example.com address that is never
// delivered anywhere.

import { expect, test } from "@playwright/test";

import { readSharedAccount, screenshotPath } from "../fixtures/shared";

test("invite a teammate and reveal the one-time link", async ({ page }) => {
  const account = readSharedAccount();

  await page.goto(`/org/${account.orgId}/members`);
  await expect(
    page.getByRole("heading", { name: "Team members" }),
  ).toBeVisible();

  await page
    .getByPlaceholder("teammate@example.com")
    .fill("new-teammate@example.com");
  await page.getByRole("button", { name: /^Create invite link$/ }).click();

  // The one-time invite link is revealed in a snippet block.
  await expect(page.getByText("Invite link (shown once)")).toBeVisible();
  const snippet = page.locator("pre.snippet");
  await expect(snippet).toContainText("/invite?token=");
  await expect(page.getByRole("button", { name: "Copy link" })).toBeVisible();

  await page.screenshot({
    path: screenshotPath("08-members-invite"),
    fullPage: true,
  });
});
