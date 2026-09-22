import enCommon from "@i18n/en/common.json";
import enConsole from "@i18n/en/console.json";
import frCommon from "@i18n/fr/common.json";
import frConsole from "@i18n/fr/console.json";
import i18next, { type i18n as I18n } from "i18next";
import { initReactI18next } from "react-i18next";

import { readLanguage } from "../lib/prefs";

export const SUPPORTED_LANGUAGES = ["en", "fr"] as const;
export type Language = (typeof SUPPORTED_LANGUAGES)[number];

export const NAMESPACES = ["common", "console"] as const;

export const resources = {
  en: { common: enCommon, console: enConsole },
  fr: { common: frCommon, console: frConsole },
} as const;

export function isLanguage(value: unknown): value is Language {
  return typeof value === "string" && (SUPPORTED_LANGUAGES as readonly string[]).includes(value);
}

/** Saved choice first, then the browser's list, then English. */
export function detectLanguage(saved: string | null, browser: readonly string[]): Language {
  if (isLanguage(saved)) return saved;
  for (const tag of browser) {
    const base = tag.split("-")[0]?.toLowerCase();
    if (isLanguage(base)) return base;
  }
  return "en";
}

export function initI18n(language?: Language): I18n {
  const browser = typeof navigator === "undefined" ? [] : [...navigator.languages];
  const lng = language ?? detectLanguage(readLanguage(), browser);

  void i18next.use(initReactI18next).init({
    resources,
    lng,
    fallbackLng: "en",
    ns: NAMESPACES,
    defaultNS: "console",
    interpolation: { escapeValue: false },
    returnNull: false,
  });

  return i18next;
}

export default i18next;
