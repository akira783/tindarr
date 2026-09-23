/** URL helpers, all of them refusing anything the console must not open or display. */

/**
 * Host of the `server` parameter of a pairing link
 * (`tindarr://pair?server=<public_url>&code=<code>`), shown next to the QR code
 * so the user sees where the phone will connect (docs/auth.md, section 9).
 * Internationalised names come back in punycode, as the app displays them.
 */
export function pairingHost(link: string): string | null {
  try {
    const server = new URL(link).searchParams.get("server");
    if (server === null) return null;
    const url = new URL(server);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    return url.host;
  } catch {
    return null;
  }
}

/**
 * The pairing link, but only when it is one the console may put in an `href`.
 *
 * The server builds that link (`tindarr://pair?server=…&code=…`) and nothing else can
 * reach this page, so this is belt and braces — the kind that costs one line: a value
 * that ever came back saying `javascript:` must not become something to click.
 */
export function pairingLinkHref(link: string): string | null {
  try {
    return new URL(link).protocol === "tindarr:" ? link : null;
  } catch {
    return null;
  }
}

/** Only a plex.tv page may be opened from the console (CSP `form-action`/navigation aside). */
export function isPlexAuthUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && (url.hostname === "plex.tv" || url.hostname.endsWith(".plex.tv"));
  } catch {
    return false;
  }
}

/** The origin the console is open on, proposed as `public_url` during setup. */
export function currentOrigin(): string {
  return globalThis.location.origin;
}

export function isLikelyPhone(): boolean {
  try {
    return globalThis.matchMedia("(pointer: coarse) and (max-width: 820px)").matches;
  } catch {
    return false;
  }
}
