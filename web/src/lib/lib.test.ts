import { afterEach, describe, expect, it, vi } from "vitest";

import { formatDateTime, secondsUntil } from "./format";
import { readLanguage, readTheme, writeLanguage, writeTheme } from "./prefs";
import { currentOrigin, isLikelyPhone, isPlexAuthUrl, pairingHost } from "./url";

describe("pairingHost", () => {
  it("reads the host the phone will connect to from a pairing link", () => {
    const link = `tindeerr://pair?server=${encodeURIComponent("https://tindeerr.example.com:8443")}&code=abc`;
    expect(pairingHost(link)).toBe("tindeerr.example.com:8443");
  });

  it("shows an internationalised host in punycode, as the app does", () => {
    const link = `tindeerr://pair?server=${encodeURIComponent("https://ünïcode.example")}&code=abc`;
    expect(pairingHost(link)).toBe("xn--ncode-cta3g.example");
  });

  it("returns null for a link without a server, an odd scheme or garbage", () => {
    expect(pairingHost("tindeerr://pair?code=abc")).toBeNull();
    expect(
      pairingHost(`tindeerr://pair?server=${encodeURIComponent("javascript:alert(1)")}&code=a`),
    ).toBeNull();
    expect(pairingHost("not a url")).toBeNull();
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
    expect(Object.keys(localStorage)).toEqual(["tindeerr.language", "tindeerr.theme"]);
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
