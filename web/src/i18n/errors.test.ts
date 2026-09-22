import enCommon from "@i18n/en/common.json";
import enConsole from "@i18n/en/console.json";
import frCommon from "@i18n/fr/common.json";
import frConsole from "@i18n/fr/console.json";
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/problem";
import { detectLanguage, initI18n } from "./index";
import { asTranslate, errorMessage, fieldErrors } from "./errors";

function flatten(value: unknown, prefix = ""): string[] {
  if (value === null || typeof value !== "object") return [prefix];
  return Object.entries(value as Record<string, unknown>).flatMap(([key, child]) =>
    flatten(child, prefix === "" ? key : `${prefix}.${key}`),
  );
}

describe("the catalogs", () => {
  it("have exactly the same keys in English and in French", () => {
    for (const [english, french] of [
      [enCommon, frCommon],
      [enConsole, frConsole],
    ] as const) {
      const en = flatten(english).sort();
      const fr = flatten(french).sort();
      expect(fr.filter((key) => !en.includes(key))).toEqual([]);
      expect(en.filter((key) => !fr.includes(key))).toEqual([]);
    }
  });

  it("never leave a message empty", () => {
    for (const catalog of [enCommon, enConsole, frCommon, frConsole]) {
      const empty = flatten(catalog).filter((key) => {
        const value = key
          .split(".")
          .reduce<unknown>(
            (node, part) => (node as Record<string, unknown> | undefined)?.[part],
            catalog,
          );
        return typeof value !== "string" || value.trim() === "";
      });
      expect(empty).toEqual([]);
    }
  });
});

describe("errorMessage", () => {
  const i18n = initI18n("en");
  const t = asTranslate(i18n.t.bind(i18n));

  it("maps every problem code to its own message", () => {
    expect(t("common:errors.invalid_credentials")).not.toBe("common:errors.invalid_credentials");
    expect(errorMessage(t, new ApiError(401, "invalid_credentials", null))).toBe(
      enCommon.errors.invalid_credentials,
    );
  });

  it("puts the wait of a rate limit in the message, in seconds", () => {
    const error = new ApiError(429, "rate_limited", {
      type: "about:blank",
      title: "slow down",
      status: 429,
      code: "rate_limited",
      retry_after_ms: 2400,
    });
    expect(errorMessage(t, error)).toContain("3");
  });

  it("translates the coarse reason of public_url_unverified", () => {
    const error = new ApiError(409, "public_url_unverified", {
      type: "about:blank",
      title: "unverified",
      status: 409,
      code: "public_url_unverified",
      reason: "proof_mismatch",
    });
    expect(errorMessage(t, error)).toContain(enCommon.publicUrlReason.proof_mismatch);
  });

  it("falls back to a generic message for an unknown code or a thrown value", () => {
    expect(errorMessage(t, new ApiError(503, "unknown", null))).toContain("503");
    expect(errorMessage(t, new Error("boom"))).toContain("0");
  });

  it("collects the field errors of a validation_error", () => {
    const error = new ApiError(400, "validation_error", {
      type: "about:blank",
      title: "invalid",
      status: 400,
      code: "validation_error",
      errors: [{ field: "url", message: "must start with http" }],
    });
    expect(fieldErrors(error)).toEqual({ url: "must start with http" });
    expect(fieldErrors(new Error("boom"))).toEqual({});
  });
});

describe("detectLanguage", () => {
  it("prefers the saved choice, then the browser, then English", () => {
    expect(detectLanguage("fr", ["en-GB"])).toBe("fr");
    expect(detectLanguage(null, ["fr-CA", "en"])).toBe("fr");
    expect(detectLanguage(null, ["de-DE"])).toBe("en");
    expect(detectLanguage("kl", [])).toBe("en");
  });
});
