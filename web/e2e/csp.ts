/**
 * Helper for the end-to-end lot (step 2e): every console page must load with
 * zero CSP violations under the real header (docs/roadmap.md, step 2, check b).
 *
 * It is written against a structural subset of Playwright's `Page`, so this
 * folder needs no browser dependency until the e2e lot adds one.
 */

export interface CspViolation {
  directive: string;
  blockedUri: string;
  url: string;
}

export interface MinimalPage {
  addInitScript: (script: string) => Promise<void>;
  evaluate: <T>(script: string) => Promise<T>;
}

export const VIOLATIONS_KEY = "__tindarrCspViolations";

/** Injected before any page script runs: records what the browser refuses. */
export const collectorScript = `(() => {
  const store = [];
  Object.defineProperty(window, ${JSON.stringify(VIOLATIONS_KEY)}, { value: store });
  document.addEventListener("securitypolicyviolation", (event) => {
    store.push({
      directive: event.effectiveDirective || event.violatedDirective,
      blockedUri: event.blockedURI,
      url: event.documentURI,
    });
  });
})();`;

export async function watchCspViolations(page: MinimalPage): Promise<void> {
  await page.addInitScript(collectorScript);
}

export async function readCspViolations(page: MinimalPage): Promise<CspViolation[]> {
  return await page.evaluate<CspViolation[]>(`window[${JSON.stringify(VIOLATIONS_KEY)}] ?? []`);
}

/** Throws with a readable list when the page reported any violation. */
export async function assertNoCspViolations(page: MinimalPage): Promise<void> {
  const violations = await readCspViolations(page);
  if (violations.length > 0) {
    const lines = violations
      .map((violation) => `${violation.directive} blocked ${violation.blockedUri} on ${violation.url}`)
      .join("\n");
    throw new Error(`CSP violations:\n${lines}`);
  }
}
