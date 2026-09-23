/**
 * The step-2 scenario against Plex — dispatched runs only.
 *
 * Plex is the one media server whose setup and sign-in do not happen on the server
 * itself: both go through a plex.tv PIN (docs/auth.md, sections 4 and 5). The console
 * creates the PIN, opens the plex.tv page in a separate window and polls with an opaque
 * handle; the user approves there.
 *
 * Here "the user" is the account that owns the throwaway server: the test reads the
 * code out of the page the console would have opened and approves it through plex.tv's
 * device-link endpoint. Everything after that — `public_url`, pairing — is the same
 * flow as `console.spec.ts`.
 *
 * `.github/scripts/e2e-plex.py` says plainly what could not be verified here; this file
 * inherits that caveat.
 */
import type { Page } from "@playwright/test";

import { AppClient, codeFromPairingLink, pkcePair } from "./api";
import { expect, test } from "./fixtures";

test.describe.configure({ mode: "serial" });

const PLEX_TV = "https://plex.tv";

/** The account token that approves the PINs, from the job's secrets. */
function accountToken(): string {
  const token = process.env["PLEX_ACCOUNT_TOKEN"] ?? "";
  expect(token, "PLEX_ACCOUNT_TOKEN is not set").not.toBe("");
  return token;
}

/**
 * The PIN code the console is waiting on.
 *
 * It is never displayed: the console opens `https://app.plex.tv/auth#?…&code=…` in
 * another window and offers the same address as a link. The link is what the test
 * reads, which is also the only thing a user could copy.
 */
async function pinCodeFromPage(page: Page): Promise<string> {
  const link = page.getByRole("link", { name: "Open the Plex page" });
  await expect(link).toBeVisible();
  const href = (await link.getAttribute("href")) ?? "";
  expect(href).toMatch(/^https:\/\/app\.plex\.tv\/auth/);
  // The parameters live in the fragment, not the query string.
  const fragment = href.slice(href.indexOf("#") + 1).replace(/^\?/, "");
  const code = new URLSearchParams(fragment).get("code");
  expect(code, `no code in ${href}`).not.toBeNull();
  return code!;
}

/**
 * Approve a PIN as the account would on plex.tv.
 *
 * `PUT /api/v2/pins/link` is not in Plex's published documentation; it is the call
 * python-plexapi's `MyPlexAccount.link()` makes, and the only way to approve a PIN
 * without a browser. A `204` means the PIN now carries an `authToken`.
 */
async function approvePlexPin(
  page: Page,
  code: string,
): Promise<void> {
  const response = await page.request.put(`${PLEX_TV}/api/v2/pins/link`, {
    headers: {
      Accept: "application/json",
      "Content-Type": "application/x-www-form-urlencoded",
      "X-Plex-Product": "Tindarr e2e",
      "X-Plex-Client-Identifier": "tindarr-e2e-ci",
      "X-Plex-Token": accountToken(),
    },
    form: { code },
  });
  expect(
    [200, 204].includes(response.status()),
    `plex.tv refused to link the PIN: ${response.status()} ${await response.text()}`,
  ).toBe(true);
}

/** The console opens plex.tv in another window; nothing here should follow it. */
function closePlexWindows(page: Page): void {
  page.context().on("page", (opened) => {
    void opened.close();
  });
}

test("the wizard links the owner's Plex account and finishes setup", async ({
  page,
  mediaUrl,
  setupCode,
  publicUrl,
}) => {
  closePlexWindows(page);

  await page.goto("/");
  await expect(page).toHaveURL(/\/setup$/);
  await page.getByLabel("Setup code").fill(setupCode);
  await page.getByRole("button", { name: "Continue" }).click();

  await expect(page.getByRole("heading", { name: "Connect your media server" })).toBeVisible();
  await page.getByLabel("Type").selectOption("plex");
  await page.getByLabel("Address").fill(mediaUrl);

  // There is no API key for Plex: the connector takes the owner's account token from a
  // completed PIN of purpose `owner_token` (docs/auth.md, section 5).
  await expect(page.getByLabel("Administrator API key")).toHaveCount(0);
  await page.getByRole("button", { name: "Link the owner's Plex account" }).click();
  await approvePlexPin(page, await pinCodeFromPage(page));
  await expect(page.getByText(/^Linked as /)).toBeVisible();

  await page.getByRole("button", { name: "Test and save" }).click();

  // The connector checked `machineIdentifier` against the account's resources and that
  // the account owns that server, so the only sign-in method offered is the PIN.
  await expect(page.getByRole("heading", { name: "Sign in as an administrator" })).toBeVisible();
  await page.getByRole("button", { name: "Sign in with Plex" }).click();
  await approvePlexPin(page, await pinCodeFromPage(page));

  await expect(page.getByRole("heading", { name: "Public address" })).toBeVisible();
  await page.getByLabel("Public address").fill(publicUrl);
  await page.getByRole("button", { name: "Save" }).click();

  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole("heading", { name: "Server settings" })).toBeVisible();
});

test("a phone pairs with the console and gets a working token pair", async ({
  page,
  playwright,
  baseURL,
}) => {
  closePlexWindows(page);

  await page.goto("/sign-in");
  await page.getByRole("button", { name: "Sign in with Plex" }).click();
  await approvePlexPin(page, await pinCodeFromPage(page));
  await expect(page).toHaveURL(/\/settings$/);

  await page.getByRole("link", { name: "Connect a phone" }).click();
  await page.getByRole("button", { name: "Create a QR code" }).click();
  const link = await page.getByRole("link", { name: "Open in Tindarr" }).getAttribute("href");
  const code = codeFromPairingLink(link!);

  const appContext = await playwright.request.newContext();
  try {
    const app = new AppClient(appContext, baseURL!);
    const preview = await app.previewPairing(code);
    expect(preview.server_name).not.toBe("");

    const { verifier, challenge } = pkcePair();
    const requested = await app.requestPairing(code, challenge);
    await expect(
      page.getByText(`Approve only if your phone shows ${requested.confirmation_code}`),
    ).toBeVisible();
    await page.getByRole("button", { name: "Approve" }).click();

    const result = await app.completePairing(code, verifier);
    expect((await app.me(result.access_token)).id).toBe(result.user.id);
    await expect(page.getByText("Pixel 9 connected")).toBeVisible();
  } finally {
    await appContext.dispose();
  }
});
