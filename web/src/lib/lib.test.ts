import { afterEach, describe, expect, it, vi } from "vitest";

import { formatDateTime, secondsUntil } from "./format";
import { readLanguage, readTheme, writeLanguage, writeTheme } from "./prefs";
import {
  currentOrigin,
  isLikelyPhone,
  isPlexAuthUrl,
  posterUrl,
  safeHttpUrl,
  trailerEmbedUrl,
  pairingHost,
  pairingLinkHref,
} from "./url";

describe("pairingHost", () => {
  it("reads the host the phone will connect to from a pairing link", () => {
    const link = `tindarr://pair?server=${encodeURIComponent("https://tindarr.example.com:8443")}&code=abc`;
    expect(pairingHost(link)).toBe("tindarr.example.com:8443");
  });

  it("shows an internationalised host in punycode, as the app does", () => {
    const link = `tindarr://pair?server=${encodeURIComponent("https://ünïcode.example")}&code=abc`;
    expect(pairingHost(link)).toBe("xn--ncode-cta3g.example");
  });

  it("returns null for a link without a server, an odd scheme or garbage", () => {
    expect(pairingHost("tindarr://pair?code=abc")).toBeNull();
    expect(
      pairingHost(`tindarr://pair?server=${encodeURIComponent("javascript:alert(1)")}&code=a`),
    ).toBeNull();
    expect(pairingHost("not a url")).toBeNull();
  });
});

describe("pairingLinkHref", () => {
  it("only lets the app's own scheme become something to click", () => {
    const link = `tindarr://pair?server=${encodeURIComponent("https://tindarr.example.com")}&code=abc`;
    expect(pairingLinkHref(link)).toBe(link);
    expect(pairingLinkHref("javascript:alert(1)")).toBeNull();
    expect(pairingLinkHref("https://example.com/pair")).toBeNull();
    expect(pairingLinkHref("not a url")).toBeNull();
  });
});

describe("isPlexAuthUrl", () => {
  it("only accepts an https plex.tv page", () => {
    expect(isPlexAuthUrl("https://app.plex.tv/auth#?clientID=x&code=y")).toBe(true);
    expect(isPlexAuthUrl("https://plex.tv/auth")).toBe(true);
    expect(isPlexAuthUrl("http://app.plex.tv/auth")).toBe(false);
    expect(isPlexAuthUrl("https://plex.tv.evil.example/auth")).toBe(false);
    expect(isPlexAuthUrl("javascript:alert(1)")).toBe(false);
    expect(isPlexAuthUrl("nonsense")).toBe(false);
  });
});

describe("formatDateTime", () => {
  it("formats an instant in the console's language", () => {
    const formatted = formatDateTime("2026-09-22T08:30:00Z", "en-GB");
    expect(formatted).toMatch(/2026/);
  });

  it("stays readable for a missing or invalid value", () => {
    expect(formatDateTime(null, "en")).toBe("—");
    expect(formatDateTime("not a date", "en")).toBe("—");
  });
});

describe("secondsUntil", () => {
  it("counts whole seconds and never goes negative", () => {
    const now = Date.parse("2026-09-22T10:00:00Z");
    expect(secondsUntil("2026-09-22T10:00:30Z", now)).toBe(30);
    expect(secondsUntil("2026-09-22T09:59:00Z", now)).toBe(0);
    expect(secondsUntil(null, now)).toBe(0);
  });
});

describe("display preferences", () => {
  afterEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("keeps the language and the theme, and nothing else", () => {
    writeLanguage("fr");
    writeTheme("dark");
    expect(readLanguage()).toBe("fr");
    expect(readTheme()).toBe("dark");
    expect(Object.keys(localStorage)).toEqual(["tindarr.language", "tindarr.theme"]);
  });

  it("survives storage that throws, as in a private window", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(() => {
      writeTheme("light");
    }).not.toThrow();
    expect(readTheme()).toBe("system");
    expect(readLanguage()).toBeNull();
  });
});

describe("environment helpers", () => {
  it("reports the origin the console is open on", () => {
    expect(currentOrigin()).toBe(globalThis.location.origin);
  });

  it("detects a phone-sized touch screen, and copes without matchMedia", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: true }) as MediaQueryList);
    expect(isLikelyPhone()).toBe(true);

    vi.stubGlobal("matchMedia", () => {
      throw new Error("unsupported");
    });
    expect(isLikelyPhone()).toBe(false);
    vi.unstubAllGlobals();
  });
});

describe("the poster URL", () => {
  const base = "https://image.tmdb.org/t/p/";

  it("builds a sized TMDb URL from a base and a path", () => {
    expect(posterUrl(base, "/abc123.jpg", "w342")).toBe("https://image.tmdb.org/t/p/w342/abc123.jpg");
  });

  it("has nothing to build when either half is missing", () => {
    expect(posterUrl(base, null)).toBeNull();
    expect(posterUrl(null, "/abc123.jpg")).toBeNull();
    expect(posterUrl(base, "")).toBeNull();
  });

  it("refuses a path that is not one, so no src can be pointed elsewhere", () => {
    expect(posterUrl(base, "abc.jpg")).toBeNull();
    expect(posterUrl(base, "//evil.example/x.jpg")).toBeNull();
    expect(posterUrl(base, "/../../etc/passwd")).toBeNull();
    expect(posterUrl(base, "/a.jpg?x=1")).toBeNull();
  });

  it("refuses a base that is not https, and one that is not a URL at all", () => {
    expect(posterUrl("http://image.tmdb.org/t/p/", "/a.jpg")).toBeNull();
    expect(posterUrl("not a url", "/a.jpg")).toBeNull();
  });
});

describe("the trailer URL", () => {
  it("frames the no-cookie player for a YouTube key", () => {
    const url = trailerEmbedUrl({ site: "youtube", key: "YoHD9XEInc0" });
    expect(url).toBe("https://www.youtube-nocookie.com/embed/YoHD9XEInc0?rel=0&modestbranding=1");
  });

  it("refuses anything but YouTube, and anything but a key", () => {
    expect(trailerEmbedUrl(null)).toBeNull();
    expect(trailerEmbedUrl(undefined)).toBeNull();
    expect(trailerEmbedUrl({ site: "vimeo", key: "YoHD9XEInc0" })).toBeNull();
    expect(trailerEmbedUrl({ site: "youtube", key: "../../evil" })).toBeNull();
    expect(trailerEmbedUrl({ site: "youtube", key: "a/b" })).toBeNull();
    expect(trailerEmbedUrl({ site: "youtube", key: "short" })).toBeNull();
  });
});

describe("a link the console may put in an href", () => {
  it("keeps http and https and drops everything else", () => {
    expect(safeHttpUrl("https://jellyfin.example/web/#/details")).toBe(
      "https://jellyfin.example/web/#/details",
    );
    expect(safeHttpUrl("http://192.168.1.76:8096/x")).toBe("http://192.168.1.76:8096/x");
    expect(safeHttpUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpUrl("data:text/html,<script>")).toBeNull();
    expect(safeHttpUrl(null)).toBeNull();
    expect(safeHttpUrl("nonsense")).toBeNull();
  });
});
