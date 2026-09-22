/**
 * The session's CSRF token, in memory only: never a cookie a script can read,
 * never `localStorage` (docs/adr/0009). A reload gets it back from
 * `GET /auth/web/session`.
 */
let token: string | null = null;

export function getCsrfToken(): string | null {
  return token;
}

export function setCsrfToken(value: string | null): void {
  token = value;
}
