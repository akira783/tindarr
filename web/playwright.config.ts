/**
 * End-to-end configuration (docs/roadmap.md, step 2, check b).
 *
 * `npm test` (Vitest) and this suite are deliberately separate: Vitest runs on every
 * push against a mocked API and needs no browser, while this one needs a built image,
 * real Jellyfin and Emby containers, and a Chromium download. Vitest collects
 * `e2e/**\/*.test.ts` (the pure helpers), Playwright collects `e2e/**\/*.spec.ts`.
 *
 * One **project** is one target, named after the media server it is set up against.
 * `.github/scripts/e2e-stack.sh up` writes the environment file that describes them;
 * a target whose `E2E_<NAME>_URL` is missing is simply not run, which is how the Plex
 * project stays out of every run but the one that has the secrets.
 */
import process from "node:process";

import { defineConfig } from "@playwright/test";

import type { MediaKind, TargetOptions } from "./e2e/fixtures";

const KNOWN_TARGETS = ["JELLYFIN", "JELLYFIN_MIN", "EMBY", "PLEX"] as const;

function env(name: string): string | undefined {
  const value = process.env[name];
  return value === undefined || value === "" ? undefined : value;
}

function required(name: string): string {
  const value = env(name);
  if (value === undefined) throw new Error(`${name} is not set: run e2e-stack.sh up`);
  return value;
}

const shared = {
  adminUser: env("E2E_ADMIN_USER") ?? "tindarr-admin",
  adminPassword: env("E2E_ADMIN_PASSWORD") ?? "",
  plainUser: env("E2E_USER") ?? "tindarr-user",
  plainPassword: env("E2E_USER_PASSWORD") ?? "",
};

const projects = KNOWN_TARGETS.filter((name) => env(`E2E_${name}_URL`) !== undefined).map(
  (name) => {
    const baseURL = required(`E2E_${name}_URL`);
    const options: TargetOptions = {
      ...shared,
      mediaKind: (env(`E2E_${name}_KIND`) ?? name.toLowerCase().split("_")[0]) as MediaKind,
      mediaUrl: env(`E2E_${name}_MEDIA_URL`) ?? "",
      mediaApiKey: env(`E2E_${name}_API_KEY`) ?? "",
      setupCode: required(`E2E_${name}_SETUP_CODE`),
      // The console is reached at a loopback address, and that same address is what
      // phones would use, so it is also the `public_url` the scenario sets.
      publicUrl: env(`E2E_${name}_PUBLIC_URL`) ?? baseURL,
    };
    return {
      name: name.toLowerCase().replace("_", "-"),
      // Plex is set up and signed in to through plex.tv, not a password: its own file.
      testMatch: options.mediaKind === "plex" ? "**/plex.spec.ts" : "**/console.spec.ts",
      use: { baseURL, ...options },
    };
  },
);

if (projects.length === 0) {
  throw new Error("no end-to-end target: run .github/scripts/e2e-stack.sh up and source e2e.env");
}

export default defineConfig<TargetOptions>({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  outputDir: "test-results",
  // The scenario sets a server up once and then signs in to it: the order matters and
  // a second attempt would meet a server that is already claimed. Nothing is retried
  // and nothing runs in parallel, on purpose.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: env("CI") !== undefined,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: env("CI") !== undefined ? [["github"], ["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    actionTimeout: 20_000,
    // The console refuses a Host it does not serve, and a loopback Host is what makes
    // its `__Host-` cookies work over plain HTTP (docs/auth.md, sections 1 and 2).
    ignoreHTTPSErrors: false,
    locale: "en-GB",
  },
  projects,
});
