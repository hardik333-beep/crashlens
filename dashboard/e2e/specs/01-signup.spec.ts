// Flow (a): sign up through the UI form and land on the organization view.
//
// This is the only spec that drives the sign-in/up FORM by hand: it exists to
// prove and screenshot the real signup experience. Every other spec logs in via
// the injected API token (see e2e/setup/auth.setup.ts). It signs up a fresh
// account of its own, so it never disturbs the shared instance-admin account.

import { expect, test } from "@playwright/test";

import {
  screenshotPath,
  STRONG_PASSWORD,
  uniqueEmail,
} from "../fixtures/shared";

test("sign up and reach the organization projects view", async ({ page }) => {
  const email = uniqueEmail("e2e-signup");

  await page.goto("/signup");
  await expect(
    page.getByRole("heading", { name: "Create your account" }),
  ).toBeVisible();

  await page.locator('input[type="email"]').fill(email);
  await page.locator('input[type="password"]').fill(STRONG_PASSWORD);
  await page.locator('input[type="text"]').fill("Northwind Traders");

  await page.getByRole("button", { name: /^Create account$/ }).click();

  // Signup navigates to "/", and a fresh account with exactly one organization
  // is redirected straight into that org's projects list.
  await page.waitForURL(/\/org\/[^/]+\/projects$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();
  // A brand-new project list is empty; the empty state confirms we are really
  // signed in and looking at this account's own (empty) workspace.
  await expect(page.locator(".empty-title")).toContainText("No projects yet.");

  await page.screenshot({ path: screenshotPath("01-signup"), fullPage: true });
});
