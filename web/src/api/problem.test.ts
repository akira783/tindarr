import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import process from "node:process";

import { describe, expect, it } from "vitest";

import { ApiError, PROBLEM_CODES, isProblemCode, toApiError } from "./problem";

/** The codes the contract lists in `Problem.code`, read from the contract itself. */
function codesFromContract(): string[] {
  // Vitest runs from web/, so the contract sits next to it.
  const contract = readFileSync(resolve(process.cwd(), "../api/openapi.yaml"), "utf8");
  const start = contract.indexOf("Stable identifier. Known values:");
  expect(start).toBeGreaterThan(0);
  const end = contract.indexOf("examples: [llm_quota]", start);
  const block = contract.slice(start, end);
  // Drop the sentence and the category labels ("- sign-in:"), keep the codes.
  const listed = block
    .split("\n")
    .slice(1)
    .map((line) => line.replace(/^\s*-\s+[a-z][a-z\s-]*:\s*/, " "))
    .join(" ");
  return listed.split(/[\s,;.]+/).filter((token) => /^[a-z][a-z0-9_]*$/.test(token));
}

describe("problem codes", () => {
  it("match the contract, so an added code is never silently unhandled", () => {
    const fromContract = new Set(codesFromContract());
    const known = new Set<string>(PROBLEM_CODES);

    expect([...fromContract].filter((code) => !known.has(code))).toEqual([]);
    expect([...known].filter((code) => !fromContract.has(code))).toEqual([]);
  });

  it("recognises a known code and rejects anything else", () => {
    expect(isProblemCode("csrf_failed")).toBe(true);
    expect(isProblemCode("not_a_code")).toBe(false);
    expect(isProblemCode(42)).toBe(false);
  });
});

describe("toApiError", () => {
  it("keeps the problem details of a known code", () => {
    const error = toApiError(429, {
      type: "about:blank",
      title: "rate limited",
      status: 429,
      code: "rate_limited",
      retry_after_ms: 2500,
    });

    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("rate_limited");
    expect(error.retryAfterMs).toBe(2500);
    expect(error.is("rate_limited", "unknown")).toBe(true);
  });

  it("falls back to `unknown` for an unknown code or a body that is not a problem", () => {
    expect(toApiError(500, { code: "brand_new" }).code).toBe("unknown");
    expect(toApiError(502, "<html>").code).toBe("unknown");
    expect(toApiError(500, null).problem).toBeNull();
  });

  it("exposes the field errors and the coarse reason", () => {
    const validation = toApiError(400, {
      type: "about:blank",
      title: "invalid",
      status: 400,
      code: "validation_error",
      errors: [{ field: "public_url", message: "must be an origin" }],
    });
    expect(validation.fieldErrors).toEqual([{ field: "public_url", message: "must be an origin" }]);

    const unverified = toApiError(409, {
      type: "about:blank",
      title: "unverified",
      status: 409,
      code: "public_url_unverified",
      reason: "proof_mismatch",
    });
    expect(unverified.reason).toBe("proof_mismatch");
  });
});
