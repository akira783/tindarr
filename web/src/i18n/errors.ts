import enCommon from "@i18n/en/common.json";

import { isApiError, type ErrorCode } from "../api/problem";

/** Minimal shape of i18next's `t`, so this helper stays easy to call and to test. */
export type Translate = (key: string, options?: Record<string, unknown>) => string;

/**
 * i18next types `t` with the exact key union, which cannot accept a key built
 * at run time from a problem `code`. The catalogs are checked instead: the type
 * assertion just above `everyCodeHasAMessage` proves every code has a message,
 * and `errors.test.ts` proves French matches English.
 */
export function asTranslate(t: unknown): Translate {
  return t as Translate;
}

type NeededKeys = `errors.${ErrorCode}`;
type CatalogKeys = `errors.${keyof (typeof enCommon)["errors"]}`;

/**
 * Compile-time proof that every problem code the contract documents has an
 * English message; `src/i18n/errors.test.ts` checks French against English.
 */
export const everyCodeHasAMessage: NeededKeys extends CatalogKeys ? true : never = true;

const REASONS = new Set(Object.keys(enCommon.publicUrlReason));

/** Turns any thrown value into a message the user can act on. */
export function errorMessage(t: Translate, error: unknown): string {
  if (!isApiError(error)) {
    return t("common:errors.unknown", { status: 0 });
  }

  if (error.code === "rate_limited") {
    const seconds = Math.max(1, Math.ceil((error.retryAfterMs ?? 1000) / 1000));
    return t("common:errors.rate_limited", { seconds });
  }

  if (error.code === "public_url_unverified") {
    const reason = error.reason ?? "";
    return t("common:errors.public_url_unverified", {
      reason: REASONS.has(reason) ? t(`common:publicUrlReason.${reason}`) : reason,
    });
  }

  if (error.code === "unknown") {
    return t("common:errors.unknown", { status: error.status });
  }

  return t(`common:errors.${error.code}`);
}

/** Field errors of a `validation_error`, keyed by field name. */
export function fieldErrors(error: unknown): Record<string, string> {
  if (!isApiError(error)) return {};
  const result: Record<string, string> = {};
  for (const item of error.fieldErrors) {
    result[item.field] = item.message;
  }
  return result;
}
