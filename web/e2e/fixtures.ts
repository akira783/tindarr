/**
 * Playwright fixtures for the end-to-end scenario (docs/roadmap.md, step 2, check b).
 *
 * One Playwright *project* is one target: a Tindarr server that has never been set up,
 * next to the media server it will be configured against. `.github/scripts/e2e-stack.sh`
 * starts them and writes the environment file `playwright.config.ts` reads.
 *
 * Every test carries a guard that fails it if the browser refused anything under the
 * console's Content-Security-Policy, or if a script threw.
 */
import { test as base, expect } from "@playwright/test";

import { REPORT_BINDING, readCspViolations, watchCspViolations, type CspViolation } from "./csp";

export type MediaKind = "jellyfin" | "emby" | "plex";

/** Set per project in `playwright.config.ts`, from the stack's environment file. */
export interface TargetOptions {
  mediaKind: MediaKind;
  mediaUrl: string;
  mediaApiKey: string;
  setupCode: string;
  publicUrl: string;
  adminUser: string;
  adminPassword: string;
  plainUser: string;
  plainPassword: string;
}

/**
 * Console messages that mean the browser refused something under the CSP.
 *
 * Chromium logs every failed request as a console error ("Failed to load resource: the
 * server responded with a status of 401"), and the console makes such requests on
 * purpose — `GET /auth/web/session` while nobody is signed in is one. Failing on those
 * would be failing on the scenario itself, so this assertion names what it is about.
 * Uncaught exceptions are a separate, unconditional failure.
 */
const CSP_MARKERS = ["content security policy", "trusted type", "trustedscript"];

function isCspMessage(text: string): boolean {
  const lowered = text.toLowerCase();
  return CSP_MARKERS.some((marker) => lowered.includes(marker));
}

function describe(violation: CspViolation): string {
  return `${violation.directive} blocked ${violation.blockedUri} on ${violation.url}`;
}

// `void` is how Playwright types a fixture that yields nothing, which this guard does.
// eslint-disable-next-line @typescript-eslint/no-invalid-void-type
export const test = base.extend<TargetOptions & { cspGuard: void }>({
  mediaKind: ["jellyfin", { option: true }],
  mediaUrl: ["", { option: true }],
  mediaApiKey: ["", { option: true }],
  setupCode: ["", { option: true }],
  publicUrl: ["", { option: true }],
  adminUser: ["", { option: true }],
  adminPassword: ["", { option: true }],
  plainUser: ["", { option: true }],
  plainPassword: ["", { option: true }],

  cspGuard: [
    async ({ page, baseURL }, use, testInfo) => {
      const violations: string[] = [];
      const cspMessages: string[] = [];
      const consoleErrors: string[] = [];
      const pageErrors: string[] = [];

      // The binding outlives navigations; the in-page array only lives as long as its
      // document, so both are read (docs/adr/0009: the console runs under a strict CSP).
      await page.exposeFunction(REPORT_BINDING, (violation: CspViolation) => {
        violations.push(describe(violation));
      });
      await watchCspViolations(page);

      // Only this origin's frames. The deck embeds a third party's player, and
      // its own policy and its own exceptions are not the console's — the CSP
      // collector is scoped the same way (see `csp.ts`).
      const ours = (url: string | undefined): boolean =>
        baseURL === undefined || url === undefined || url.startsWith(baseURL);

      page.on("console", (message) => {
        if (message.type() !== "error") return;
        const where = message.location().url;
        if (!ours(where)) return;
        const text = `${where}: ${message.text()}`;
        consoleErrors.push(text);
        if (isCspMessage(message.text())) cspMessages.push(text);
      });
      page.on("pageerror", (error) => {
        // `pageerror` carries no frame, so the stack is all there is to go on.
        const stack = error.stack ?? error.message;
        if (stack.includes("youtube")) return;
        pageErrors.push(stack);
      });

      await use();

      if (!page.isClosed()) {
        for (const violation of await readCspViolations(page)) {
          const line = describe(violation);
          if (!violations.includes(line)) violations.push(line);
        }
      }
      if (consoleErrors.length > 0) {
        await testInfo.attach("console-errors.txt", {
          body: consoleErrors.join("\n"),
          contentType: "text/plain",
        });
      }

      expect(violations, "securitypolicyviolation events").toEqual([]);
      expect(cspMessages, "console messages about the Content-Security-Policy").toEqual([]);
      expect(pageErrors, "uncaught exceptions on the page").toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };
