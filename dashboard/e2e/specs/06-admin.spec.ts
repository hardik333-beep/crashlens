// Flow (h): the instance-admin panel. The shared account is the first user on a
// fresh instance, so it is the instance administrator and can see these views.
// Runs last so the overview counts reflect the projects, keys and errors the
// earlier specs created.

import { expect, test } from "@playwright/test";

import { screenshotPath } from "../fixtures/shared";

test("instance overview shows the stat tiles", async ({ page }) => {
  await page.goto("/admin");
  await expect(
    page.getByRole("heading", { name: "Instance overview" }),
  ).toBeVisible();
  // The stat tiles render with their labels.
  await expect(page.getByText("People", { exact: true })).toBeVisible();
  await expect(page.getByText("Organizations", { exact: true })).toBeVisible();
  await expect(page.getByText("Errors tracked", { exact: true })).toBeVisible();
  await expect(page.locator(".stat-tile").first()).toBeVisible();

  await page.screenshot({
    path: screenshotPath("10-admin-overview"),
    fullPage: true,
  });
});

test("people page lists the instance users", async ({ page }) => {
  await page.goto("/admin/users");
  await expect(page.getByRole("heading", { name: "People" })).toBeVisible();
  // The column headers confirm the operator user table rendered.
  await expect(page.getByRole("columnheader", { name: "Email" })).toBeVisible();
  await expect(
    page.getByRole("columnheader", { name: "Instance admin" }),
  ).toBeVisible();

  await page.screenshot({
    path: screenshotPath("11-admin-users"),
    fullPage: true,
  });
});
