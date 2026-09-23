/**
 * The step-2 scenario, end to end, against a real media server
 * (docs/roadmap.md, step 2, check b; the flows are docs/auth.md).
 *
 * It starts on a Tindarr server that has never been set up and, in order:
 *
 * 1. claims it with the setup code, configures the media server, and completes setup
 *    with an administrator password sign-in, then sets `public_url`;
 * 2. signs a **plain user** in with a password and checks they land where a
 *    non-administrator belongs;
 * 3. (Jellyfin only) signs in with **Quick Connect**, the code approved through
 *    `POST /QuickConnect/Authorize` with a real user token;
 * 4. pairs a phone: the console creates the code, "the app" previews it, asks for it
 *    with a PKCE challenge, the console shows the confirmation code and approves, the
 *    app collects a token pair, and that token pair works.
 *
 * The tests are **serial and stateful**: each one needs the server to be in the state
 * the previous one left it in.
 */
import type { Page } from "@playwright/test";

import {
  AppClient,
  approveQuickConnect,
  codeFromPairingLink,
  mediaServerSignIn,
  pkcePair,
} from "./api";
import { expect, test } from "./fixtures";

test.describe.configure({ mode: "serial" });

/**
 * Fill the password form and submit it.
 *
 * The user name field is addressed by its role: once the server offers Quick Connect
 * too, the sign-in panel is a tab panel labelled "User name and password", which a
 * plain label lookup would match as well.
 */
async function signInWithPassword(
  page: Page,
  username: string,
  password: string,
): Promise<void> {
  await page.getByRole("textbox", { name: "User name" }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("the wizard claims the server, configures the media server and finishes setup", async ({
  page,
  mediaKind,
  mediaUrl,
  mediaApiKey,
  setupCode,
  adminUser,
  adminPassword,
  publicUrl,
}) => {
  // A server that still needs setup sends every visitor to the wizard.
  await page.goto("/");
  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByRole("heading", { name: "Set up this server" })).toBeVisible();

  await page.getByLabel("Setup code").fill(setupCode);
  await page.getByRole("button", { name: "Continue" }).click();

  await expect(page.getByRole("heading", { name: "Connect your media server" })).toBeVisible();
  await page.getByLabel("Type").selectOption(mediaKind);
  await page.getByLabel("Address").fill(mediaUrl);
  await page.getByLabel("Administrator API key").fill(mediaApiKey);
  await page.getByRole("button", { name: "Test and save" }).click();

  // The wizard moves on only when the server could really reach the media server with
  // that key and found it to be an administrator's (docs/auth.md, section 6).
  await expect(page.getByRole("heading", { name: "Sign in as an administrator" })).toBeVisible();

  await signInWithPassword(page, adminUser, adminPassword);

  // The first administrator sign-in completes setup and opens a new web session.
  await expect(page.getByRole("heading", { name: "Public address" })).toBeVisible();
  const address = page.getByLabel("Public address");
  await expect(address).toHaveValue(publicUrl);
  await address.fill(publicUrl);
  await page.getByRole("button", { name: "Save" }).click();

  // Saving it means the server asked that address for `server/info` and recognised its
  // own proof (docs/auth.md, section 10). An administrator lands on the settings.
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole("heading", { name: "Server settings" })).toBeVisible();
});

test("a plain user signs in with a password and lands on phone pairing", async ({
  page,
  plainUser,
  plainPassword,
}) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/sign-in$/);

  await signInWithPassword(page, plainUser, plainPassword);

  // Not an administrator: no settings, no users, straight to "Connect a phone".
  await expect(page).toHaveURL(/\/connect-phone$/);
  await expect(page.getByRole("heading", { name: "Connect a phone" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Server settings" })).toHaveCount(0);
});

test("an administrator signs in with Quick Connect", async ({
  page,
  request,
  mediaKind,
  mediaUrl,
  adminUser,
  adminPassword,
}) => {
  test.skip(mediaKind !== "jellyfin", "Quick Connect is a Jellyfin feature");

  // `quick_connect` only joins `auth_methods` once the background probe has read
  // `GET /QuickConnect/Enabled` (docs/auth.md, section 11). That probe runs every two
  // minutes and cannot succeed before a media server is configured, so the first run
  // that can see one may be a full interval after setup completed. The page is re-read
  // until the method is offered, with room for one whole interval.
  test.setTimeout(300_000);
  const tab = page.getByRole("tab", { name: "Quick Connect" });
  await expect(async () => {
    await page.goto("/sign-in");
    await expect(tab).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 180_000 });

  await tab.click();
  await page.getByRole("button", { name: "Use Quick Connect" }).click();

  const code = (await page.locator("p.code").innerText()).trim();
  expect(code).toMatch(/^\d{6}$/);

  // What the user does in a Jellyfin client where they are already signed in.
  const session = await mediaServerSignIn(request, mediaUrl, adminUser, adminPassword);
  await approveQuickConnect(request, mediaUrl, session, code);

  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole("heading", { name: "Server settings" })).toBeVisible();
});

test("a phone pairs with the console and gets a working token pair", async ({
  page,
  playwright,
  baseURL,
  adminUser,
  adminPassword,
}) => {
  await page.goto("/sign-in");
  await signInWithPassword(page, adminUser, adminPassword);
  await expect(page).toHaveURL(/\/settings$/);

  await page.getByRole("link", { name: "Connect a phone" }).click();
  await expect(page.getByRole("heading", { name: "Connect a phone" })).toBeVisible();
  await page.getByRole("button", { name: "Create a QR code" }).click();

  // The QR code and this link carry the same thing: the pairing link the app scans.
  const link = await page.getByRole("link", { name: "Open in Tindarr" }).getAttribute("href");
  expect(link).toMatch(/^tindarr:\/\/pair\?/);
  const code = codeFromPairingLink(link!);
  await expect(page.getByText(/^Connects to /)).toBeVisible();

  // From here on, "the app": its own context, so it sends no console cookie and no
  // CSRF token, exactly like a phone.
  const appContext = await playwright.request.newContext();
  try {
    const app = new AppClient(appContext, baseURL!);

    const preview = await app.previewPairing(code);
    expect(preview.user_name).toBe(adminUser);
    expect(preview.server_name).not.toBe("");

    const { verifier, challenge } = pkcePair();
    const requested = await app.requestPairing(code, challenge);
    expect(requested.confirmation_code).toMatch(/^\d{4}$/);

    // The console polls on its own and must now show the device and the same code.
    await expect(page.getByText("A phone is asking to connect")).toBeVisible();
    await expect(page.getByText(`Approve only if your phone shows ${requested.confirmation_code}`)).toBeVisible();
    await expect(page.getByText("Pixel 9")).toBeVisible();

    await page.getByRole("button", { name: "Approve" }).click();

    const result = await app.completePairing(code, verifier);
    expect(result.setup_completed_now).toBe(false);
    expect(result.user.name).toBe(adminUser);

    // The pair works, and rotating it gives a pair that works too.
    expect((await app.me(result.access_token)).name).toBe(adminUser);
    const rotated = await app.refresh(result.refresh_token);
    expect(rotated.refresh_token).not.toBe(result.refresh_token);
    expect((await app.me(rotated.access_token)).name).toBe(adminUser);

    await expect(page.getByText("Pixel 9 connected")).toBeVisible();
  } finally {
    await appContext.dispose();
  }
});
