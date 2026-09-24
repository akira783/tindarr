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

/**
 * A TMDb poster URL, or null when it cannot be built safely.
 *
 * The base comes from `server/info` and the path from TMDb, so neither is the
 * user's to choose — but both travel through this server and end up in a `src`
 * attribute, and the rule that keeps that harmless is cheap: the path must look
 * like a TMDb path (`/xxxx.jpg`), and the result must still be an `https` URL on
 * the host the base named. `img-src` would refuse anything else anyway; this is
 * the layer that does not depend on a browser reading the header.
 */
export function posterUrl(
  base: string | null | undefined,
  path: string | null | undefined,
  size: "w185" | "w342" | "w500" | "w1280" = "w342",
): string | null {
  if (base == null || base === "" || path == null || path === "") return null;
  if (!/^\/[A-Za-z0-9._-]+$/.test(path)) return null;
  try {
    const root = new URL(base);
    if (root.protocol !== "https:") return null;
    const url = new URL(`${size}${path}`, base);
    return url.origin === root.origin && url.pathname.startsWith(root.pathname) ? url.href : null;
  } catch {
    return null;
  }
}

/**
 * The one origin the console may frame: the trailer player (ADR 0009, roadmap 4.6).
 *
 * The server writes the same string into the policy it serves this page under
 * (`TRAILER_FRAME_ORIGIN` in `server/src/tindarr/api/console.py`). Nothing links
 * the two at build time; what catches a change to one and not the other is
 * `web/e2e/deck.spec.ts`, which frames a real trailer in a real browser.
 */
export const TRAILER_ORIGIN = "https://www.youtube-nocookie.com";

/**
 * The player URL for a trailer, or null when the key is not one.
 *
 * The key is written by TMDb, and the contract already constrains it
 * (`^[A-Za-z0-9_-]{6,20}$`). It is checked again here because this value becomes
 * the `src` of a frame: a key carrying a slash or a colon would otherwise be able
 * to point that frame at another path, or another site, on a day the contract and
 * the server disagree. `site` must be `youtube`; nothing else is embeddable.
 */
export function trailerEmbedUrl(
  trailer: { site: string; key: string } | null | undefined,
): string | null {
  if (trailer?.site !== "youtube") return null;
  if (!/^[A-Za-z0-9_-]{6,20}$/.test(trailer.key)) return null;
  const url = new URL(`${TRAILER_ORIGIN}/embed/${trailer.key}`);
  // `rel=0` keeps the suggestions that follow inside the same channel, and
  // `modestbranding` drops the logo overlay. Autoplay is deliberately not asked
  // for: the frame is only built after a click, and a click is not consent to sound.
  url.searchParams.set("rel", "0");
  url.searchParams.set("modestbranding", "1");
  return url.href;
}

/**
 * A link the console may put in an `href`, or null.
 *
 * `watch_url` on a like is a deep link built by the media server adapter, which
 * means it is a string this console did not write. Only `http` and `https` ever
 * become something to click; a `javascript:` or `data:` one is dropped.
 */
export function safeHttpUrl(value: string | null | undefined): string | null {
  if (value == null || value === "") return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}
