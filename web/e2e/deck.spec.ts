/**
 * The deck, in a real browser, against the built server (roadmap 4.6).
 *
 * It runs **after** `console.spec.ts`, which is what claims the server and creates
 * the users; Playwright orders files by path and this project runs one worker, so
 * "console" before "deck" is alphabetical rather than lucky — but it is a real
 * dependency, and moving either file means checking it again.
 *
 * Two shapes, and which one runs depends on what the environment holds:
 *
 * - **no keys** (CI, and anybody's checkout): the deck cannot be generated at all,
 *   which is a state the page has to handle rather than a reason not to test it.
 *   The page must say what to do next and offer the connectors page;
 * - **`E2E_TMDB_KEY` and `E2E_LLM_BASE_URL` set** (the author's own run, against a
 *   local model that costs nothing): a real batch, a real card, a real verdict, a
 *   real undo, and the trailer frame actually loaded from
 *   `youtube-nocookie.com` — which is the one thing no mock can prove, because it
 *   is the browser reading the server's own Content-Security-Policy that decides.
 *
 * As everywhere in this folder, the fixture fails the test on any
 * `securitypolicyviolation`, any console message about the CSP and any uncaught
 * exception.
 */
import process from "node:process";

import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

const TMDB_KEY = process.env["E2E_TMDB_KEY"] ?? "";
const LLM_BASE_URL = process.env["E2E_LLM_BASE_URL"] ?? "";
const LLM_MODEL = process.env["E2E_LLM_MODEL"] ?? "";
const LLM_PROVIDER = process.env["E2E_LLM_PROVIDER"] ?? "openai_compatible";
const LLM_API_KEY = process.env["E2E_LLM_API_KEY"] ?? "not-needed";

const REAL = TMDB_KEY !== "" && LLM_BASE_URL !== "";

test.describe.configure({ mode: "serial" });

async function signIn(page: Page, username: string, password: string): Promise<void> {
  await page.goto("/sign-in");
  await page.getByRole("textbox", { name: "User name" }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/settings$/);
}

/**
 * Configure TMDb and the model through the API, with the console's own session.
 *
 * The connectors page has its own tests; what this file is about is the deck, and
 * filling three forms again would only add ways for it to fail for another reason.
 * The session cookie is copied out of the browser and sent by hand: it is `Secure`
 * even on loopback — which browsers accept and Playwright's own request context,
 * like every cookie jar that goes by the scheme alone, does not.
 */
async function configureConnectors(page: Page, baseURL: string): Promise<void> {
  // Every cookie of the context: asking for the ones of an `http://` URL hides the
  // `Secure` session cookie that the browser is nonetheless sending on loopback.
  const cookies = await page.context().cookies();
  const cookie = cookies.map((one) => `${one.name}=${one.value}`).join("; ");

  const session = await page.request.get("/api/v1/auth/web/session", {
    headers: { Cookie: cookie },
  });
  expect(session.ok(), await session.text()).toBe(true);
  const { csrf_token: csrf } = (await session.json()) as { csrf_token: string };
  const headers = { "X-CSRF-Token": csrf, Origin: baseURL, Cookie: cookie };

  const tmdb = await page.request.put("/api/v1/admin/connectors/tmdb", {
    headers,
    data: { connector: "tmdb", api_key: TMDB_KEY },
  });
  expect(tmdb.ok(), await tmdb.text()).toBe(true);

  const llm = await page.request.put("/api/v1/admin/connectors/llm", {
    headers,
    data: {
      connector: "llm",
      provider: LLM_PROVIDER,
      base_url: LLM_BASE_URL,
      model: LLM_MODEL,
      api_key: LLM_API_KEY,
    },
  });
  expect(llm.ok(), await llm.text()).toBe(true);

  const settings = await page.request.patch("/api/v1/admin/settings", {
    headers,
    data: { streaming_region: "FR" },
  });
  expect(settings.ok(), await settings.text()).toBe(true);
}

test("a deck nothing can generate says what to do next", async ({
  page,
  adminUser,
  adminPassword,
}) => {
  test.skip(REAL, "this target has real keys: the deck is checked with cards instead");

  await signIn(page, adminUser, adminPassword);
  await page.getByRole("link", { name: "Swipe" }).click();
  await expect(page).toHaveURL(/\/deck$/);

  await expect(page.getByRole("heading", { name: "Swipe" })).toBeVisible();
  await expect(page.getByText("No AI provider yet")).toBeVisible();
  await expect(page.getByRole("link", { name: "Connectors" }).last()).toBeVisible();
  // No card, so nothing to vote on: the buttons are not there to be pressed.
  await expect(page.getByRole("button", { name: /^Like/ })).toHaveCount(0);
});

test("a real batch: a card, a verdict, an undo and the trailer", async ({
  page,
  baseURL,
  adminUser,
  adminPassword,
}) => {
  test.skip(!REAL, "no TMDb key or AI provider for this target");
  // A first batch is a model call: the deck polls, and so does this.
  test.setTimeout(240_000);

  await signIn(page, adminUser, adminPassword);
  await configureConnectors(page, baseURL!);

  await page.goto("/deck");
  // No assertion on "Building your deck…": a server that already has an unvoted
  // batch answers `200` at once, and a test that needs the spinner is a test that
  // fails on the second run.
  const title = page.locator("#deck-card-title");
  await expect(title).toBeVisible({ timeout: 180_000 });
  const first = (await title.textContent()) ?? "";
  expect(first.trim()).not.toBe("");

  // Everything the card promises is on it.
  await expect(page.getByRole("heading", { name: "Why this one" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Where to watch" })).toBeVisible();
  await expect(page.locator("img.poster").first()).toBeVisible();

  // The trailer: the frame is built by the click, and the browser has to accept it
  // under the console's own policy. `frame-src` is the whole point of this line.
  await page.getByRole("button", { name: "Watch the trailer" }).click();
  const frame = page.locator("iframe.trailer-frame");
  await expect(frame).toHaveAttribute("src", /^https:\/\/www\.youtube-nocookie\.com\/embed\//);
  await expect(frame).toBeVisible();

  // A verdict from the keyboard, then the card that follows it.
  await page.keyboard.press("ArrowLeft");
  await expect(title).not.toHaveText(first, { timeout: 60_000 });

  // And back: undo puts the card that was just judged on top of the deck again.
  await page.getByRole("button", { name: /^Undo/ }).click();
  await expect(title).toHaveText(first, { timeout: 60_000 });

  // A phone's width in a browser: everything still fits, with nothing to scroll
  // sideways for.
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(title).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});
