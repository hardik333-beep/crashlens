// Auth setup project: create the shared instance-admin account through the API,
// then stash a Playwright storageState that logs every spec in as that account.
//
// This is the API-token login pattern the house standard mandates: instead of
// driving the sign-in FORM in every test, we fetch a real session token once via
// POST /api/auth/signup and inject it into localStorage under the app's own key.
//
// Because CI runs this against a FRESH database and the setup project runs before
// any spec, this signup is the first-ever user, which the server makes the
// instance administrator (server/app/accounts.py). That is what lets the admin
// panel specs see the instance views.

import { writeFileSync } from "node:fs";

import { expect, test as setup } from "@playwright/test";

import {
  ensureDir,
  STORAGE_STATE_PATH,
  STRONG_PASSWORD,
  tokenStorageState,
  uniqueEmail,
  writeSharedAccount,
} from "../fixtures/shared";
import { join } from "node:path";

setup("create the shared instance-admin account", async ({ request }) => {
  const email = uniqueEmail("e2e-admin");

  const response = await request.post("/api/auth/signup", {
    data: { email, password: STRONG_PASSWORD, org_name: "Acme Payments" },
  });
  expect(
    response.status(),
    `signup should return 201, body: ${await response.text()}`,
  ).toBe(201);

  const body = (await response.json()) as {
    token: string;
    user: { id: string; email: string };
    org: { id: string; name: string; slug: string; role: string };
  };
  expect(body.token).toBeTruthy();
  expect(body.org.id).toBeTruthy();

  // Prove the token actually authenticates AND that this account is the instance
  // admin (first user on a fresh instance) before any spec depends on it.
  const me = await request.get("/api/auth/me", {
    headers: { Authorization: `Bearer ${body.token}` },
  });
  expect(me.status(), await me.text()).toBe(200);
  const meBody = (await me.json()) as { is_instance_admin: boolean };
  expect(
    meBody.is_instance_admin,
    "the first signup on a fresh instance must be the instance admin",
  ).toBe(true);

  ensureDir(join(STORAGE_STATE_PATH, ".."));
  writeFileSync(
    STORAGE_STATE_PATH,
    JSON.stringify(tokenStorageState(body.token), null, 2),
    "utf-8",
  );
  writeSharedAccount({
    token: body.token,
    userId: body.user.id,
    orgId: body.org.id,
    email,
  });
});
