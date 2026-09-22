/**
 * Display preferences only. No credential is ever written here: the session
 * token lives in an `HttpOnly` cookie and the CSRF token in memory
 * (docs/adr/0009). Every access is guarded: storage can be unavailable or throw.
 */

const LANGUAGE_KEY = "tindarr.language";
const THEME_KEY = "tindarr.theme";

export type ThemeChoice = "system" | "light" | "dark";

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // A private window or blocked storage: the choice simply does not persist.
  }
}

export function readLanguage(): string | null {
  return read(LANGUAGE_KEY);
}

export function writeLanguage(value: string): void {
  write(LANGUAGE_KEY, value);
}

export function readTheme(): ThemeChoice {
  const value = read(THEME_KEY);
  return value === "light" || value === "dark" ? value : "system";
}

export function writeTheme(value: ThemeChoice): void {
  write(THEME_KEY, value);
}
