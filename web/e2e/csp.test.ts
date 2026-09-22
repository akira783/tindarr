import { describe, expect, it, vi } from "vitest";

import {
  VIOLATIONS_KEY,
  assertNoCspViolations,
  collectorScript,
  readCspViolations,
  watchCspViolations,
  type CspViolation,
  type MinimalPage,
} from "./csp";

function fakePage(violations: CspViolation[]): MinimalPage & { scripts: string[] } {
  const scripts: string[] = [];
  return {
    scripts,
    addInitScript: (script: string) => {
      scripts.push(script);
      return Promise.resolve();
    },
    evaluate: <T,>() => Promise.resolve(violations as T),
  };
}

describe("the end-to-end CSP helper", () => {
  it("installs a collector before the page's own scripts run", async () => {
    const page = fakePage([]);
    await watchCspViolations(page);

    expect(page.scripts).toEqual([collectorScript]);
    expect(collectorScript).toContain(VIOLATIONS_KEY);
    expect(collectorScript).toContain("securitypolicyviolation");
  });

  it("passes when the page reported nothing", async () => {
    await expect(assertNoCspViolations(fakePage([]))).resolves.toBeUndefined();
  });

  it("fails with the directive and the blocked address", async () => {
    const page = fakePage([
      { directive: "script-src", blockedUri: "inline", url: "https://tindarr.example/settings" },
    ]);
    await expect(assertNoCspViolations(page)).rejects.toThrow(
      /script-src blocked inline on https:\/\/tindarr.example\/settings/,
    );
  });

  it("reads back what the collector stored", async () => {
    const violations = [{ directive: "style-src", blockedUri: "inline", url: "/" }];
    const page = fakePage(violations);
    const evaluate = vi.spyOn(page, "evaluate");

    await expect(readCspViolations(page)).resolves.toEqual(violations);
    expect(evaluate).toHaveBeenCalledWith(expect.stringContaining(VIOLATIONS_KEY));
  });
});
