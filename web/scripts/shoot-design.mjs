/**
 * Before/after screenshots of the console, for the refonte (design/brief.md).
 *
 * Serves a built `web/dist` on a loopback port and drives it with Playwright
 * against a stubbed API, so the same pages are shot the same way before and
 * after the redesign. Posters are drawn locally rather than fetched from TMDb:
 * the shots have to be reproducible and offline.
 *
 * Usage (from web/): node scripts/shoot-design.mjs <before|after>
 */
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join, extname, resolve } from "node:path";
import process from "node:process";

import { chromium } from "@playwright/test";

const label = process.argv[2];
if (label !== "before" && label !== "after") {
  console.error("usage: node scripts/shoot-design.mjs <before|after>");
  process.exit(2);
}

const here = resolve(new URL(".", import.meta.url).pathname);
const dist = resolve(here, "../dist");
const outDir = resolve(here, "../../design/screenshots", label);

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
  ".txt": "text/plain; charset=utf-8",
};

const server = createServer(async (request, response) => {
  const path = new URL(request.url ?? "/", "http://localhost").pathname;
  const candidate = join(dist, path);
  const file = path !== "/" && existsSync(candidate) && !candidate.endsWith("/") ? candidate : join(dist, "index.html");
  const body = await readFile(file);
  response.writeHead(200, {
    "Content-Type": TYPES[extname(file)] ?? "application/octet-stream",
    // The same policy the server sends (tindarr.api.console.CONSOLE_CSP), so a
    // violation introduced by the redesign shows up here too.
    "Content-Security-Policy":
      "default-src 'self'; script-src 'self'; style-src 'self'; " +
      "img-src 'self' https://image.tmdb.org; connect-src 'self'; font-src 'self'; " +
      "frame-src https://www.youtube-nocookie.com; object-src 'none'; base-uri 'none'; " +
      "form-action 'self'; frame-ancestors 'none'; require-trusted-types-for 'script'",
  });
  response.end(body);
});
await new Promise((ready) => server.listen(0, "127.0.0.1", ready));
const origin = `http://127.0.0.1:${server.address().port}`;

// --- what the stubbed server answers -------------------------------------------------

const user = { id: "u1", name: "akira", role: "admin", media_server_admin: true };
const provider = (id, name, offer, subscribed) => ({
  provider_id: id,
  name,
  logo_path: null,
  offer,
  subscribed,
});

const cards = [
  {
    id: "card-1",
    media_type: "movie",
    tmdb_id: 815, 
    title: "Aftersun",
    original_title: "Aftersun",
    year: 2022,
    overview:
      "Vingt ans plus tard, Sophie repense aux vacances passées en Turquie avec son père quand elle avait onze ans, et à ce qu'elle ne voyait pas encore de lui.",
    genres: ["Drame"],
    runtime_minutes: 102,
    seasons: null,
    poster_path: "/aftersun.jpg",
    backdrop_path: "/aftersun-backdrop.jpg",
    ratings: { tmdb: 7.7, imdb: 7.6, rotten_tomatoes: 96, metacritic: 95 },
    providers: [provider(11, "MUBI", "subscription", true), provider(381, "Canal+", "rent", false)],
    trailer: { site: "youtube", key: "YoHD9XEInc0", name: "Bande-annonce", language: "fr" },
    rationale:
      "Vous notez très haut les drames retenus comme Past Lives, mais vous n'avez presque rien vu de britannique récent. C'est le pari de ce lot.",
    pick_type: "explore",
    availability: "none",
    expires_at: "2126-09-25T10:00:00Z",
  },
  {
    id: "card-2",
    media_type: "tv",
    tmdb_id: 95396,
    title: "Severance",
    original_title: "Severance",
    year: 2022,
    overview: "Des employés acceptent de séparer chirurgicalement leurs souvenirs de travail et leur vie privée.",
    genres: ["Drame", "Science-Fiction"],
    runtime_minutes: null,
    seasons: 2,
    poster_path: "/severance.jpg",
    backdrop_path: null,
    ratings: { tmdb: 8.4, imdb: 8.7, rotten_tomatoes: 97, metacritic: null },
    providers: [provider(350, "Apple TV+", "subscription", true)],
    trailer: null,
    rationale: "Rien de ce genre dans vos votes, mais vous avez tout aimé de The Office. Même bureau, autre lumière.",
    pick_type: "safe",
    availability: "available",
    expires_at: "2126-09-25T10:00:00Z",
  },
];

const ROUTES = [
  ["/api/v1/server/info", {
    name: "Tindarr",
    version: "1.0.0",
    api_version: 1,
    min_app_version: "1.0.0",
    setup_required: false,
    media_server: { kind: "jellyfin", name: "Jellyfin" },
    auth_methods: ["password"],
    capabilities: [],
    tmdb_image_base_url: "https://image.tmdb.org/t/p/",
  }],
  ["/api/v1/auth/web/session", {
    kind: "web",
    csrf_token: "csrf",
    expires_at: "2126-09-29T10:00:00Z",
    reauth_expires_at: "2126-09-29T10:00:00Z",
    user,
  }],
  ["/api/v1/swipe/status", {
    llm_configured: true,
    llm_provider: "ChatMock",
    tmdb_configured: true,
    requests_enabled: true,
    media_history: true,
    streaming_region: "FR",
    ratings_enabled: true,
    votes: 12,
    calibration: { done: 4, target: 15, complete: false },
    profile_ready: true,
    generations_left_today: 4,
  }],
  ["/api/v1/swipe/preferences", {
    media_type: "both",
    novelty: "bold",
    auto_request: false,
    language: "fr-FR",
    streaming_services: [11],
  }],
  ["/api/v1/swipe/deck", {
    mode: "normal",
    novelty: "bold",
    cards,
    calibration: { done: 4, target: 15, complete: false },
  }],
  ["/api/v1/swipe/likes", { likes: [] }],
  ["/api/v1/swipe/stats", {
    total: 42,
    likes: 15,
    dislikes: 12,
    seen_liked: 8,
    seen_disliked: 4,
    skips: 3,
    requested: 6,
    like_rate: 0.36,
    request_rate: 0.14,
    by_pick_type: { safe: { total: 26, likes: 11 }, explore: { total: 16, likes: 4 } },
  }],
  ["/api/v1/swipe/profile", {
    profile: {
      text: "Aime : les épopées longues, sans cynisme.\nÉvite : les comédies romantiques.\nNuances : accepte les films lents si la photo tient.",
      user_edited: false,
      updated_at: "2026-09-23T20:00:00Z",
      votes_since_update: 3,
    },
    refreshing: false,
    refresh_error: null,
  }],
  ["/api/v1/admin/connectors", {
    connectors: [
      { kind: "media_server", configured: true, secret: { set: true }, locked_fields: [], status: { health: "ok" }, url: "http://jellyfin.local:8096", server_type: "jellyfin" },
      { kind: "requests", configured: true, secret: { set: true }, locked_fields: [], status: { health: "ok" }, url: "http://seerr.local:5055" },
      { kind: "tmdb", configured: true, secret: { set: true }, locked_fields: [], status: { health: "ok" } },
      { kind: "omdb", configured: false, secret: { set: false }, locked_fields: [], status: { health: "not_configured" } },
      { kind: "llm", configured: true, secret: { set: true }, locked_fields: [], status: { health: "degraded", detail: "Le dernier appel a dépassé le délai." }, provider: "openai_compatible", model: "gpt-5.6-luna", base_url: "http://127.0.0.1:8000/v1" },
    ],
  }],
  ["/api/v1/admin/users", { users: [
    { id: "u1", name: "akira", role: "admin", enabled: true, media_server_admin: true, promoted: false, remote_access: true, created_at: "2026-01-01T10:00:00Z", last_sign_in_at: "2026-09-24T18:00:00Z", votes: 42, generations_today: 2 },
    { id: "u2", name: "camille", role: "user", enabled: true, media_server_admin: false, promoted: false, remote_access: true, created_at: "2026-03-04T10:00:00Z", last_sign_in_at: "2026-09-22T21:00:00Z", votes: 17, generations_today: 0 },
  ] }],
  ["/api/v1/admin/settings", {
    name: "Tindarr",
    public_url: "https://tindarr.example.com",
    password_sign_in: "enabled",
    language: "fr-FR",
    streaming_region: "FR",
    daily_generation_limit: 20,
    warm_up_enabled: true,
    content_filters: { exclude_adult: true, min_year: null, excluded_genres: [] },
    locked_fields: [],
  }],
  ["/api/v1/me/sessions", { sessions: [] }],
  ["/api/v1/swipe/imports", { imports: [] }],
];

function posterSvg(text, w, h) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#3a2c22"/><stop offset="0.55" stop-color="#6d4a2f"/><stop offset="1" stop-color="#17120e"/>
  </linearGradient></defs>
  <rect width="${w}" height="${h}" fill="url(#g)"/>
  <circle cx="${w * 0.5}" cy="${h * 0.34}" r="${w * 0.19}" fill="#f2b35a" opacity="0.85"/>
  <rect x="0" y="${h * 0.66}" width="${w}" height="${h * 0.34}" fill="#0f0d0b" opacity="0.55"/>
  <text x="${w * 0.5}" y="${h * 0.82}" fill="#f3eee7" font-family="Georgia, serif" font-size="${Math.round(w * 0.11)}" text-anchor="middle">${text}</text>
</svg>`;
}

const SHOTS = [
  { name: "deck", url: "/deck" },
  { name: "connectors", url: "/connectors" },
];
const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "phone", width: 390, height: 844 },
];
const THEMES = ["dark", "light"];

const browser = await chromium.launch();
const violations = [];
for (const theme of THEMES) {
  for (const viewport of VIEWPORTS) {
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      deviceScaleFactor: viewport.name === "phone" ? 2 : 1,
      colorScheme: theme,
      locale: "fr-FR",
      reducedMotion: "reduce",
    });
    await context.addInitScript(`try {
      localStorage.setItem("tindarr.language", "fr");
      localStorage.setItem("tindarr.theme", ${JSON.stringify(theme)});
    } catch {}
    document.addEventListener("securitypolicyviolation", (event) => {
      (window.__shotCsp ??= []).push((event.effectiveDirective || event.violatedDirective) + " blocked " + event.blockedURI);
    });`);

    await context.route("https://image.tmdb.org/**", async (route) => {
      const url = route.request().url();
      const name = url.includes("backdrop") ? "" : url.split("/").pop().replace(".jpg", "");
      const wide = url.includes("backdrop");
      await route.fulfill({
        status: 200,
        contentType: "image/svg+xml",
        body: posterSvg(name, wide ? 1280 : 342, wide ? 720 : 513),
      });
    });
    await context.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      const match = ROUTES.find(([routePath]) => routePath === path);
      if (match === undefined) {
        await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(match[1]),
      });
    });

    const page = await context.newPage();
    for (const shot of SHOTS) {
      await page.goto(`${origin}${shot.url}`, { waitUntil: "networkidle" });
      await page.waitForTimeout(900);
      await page.screenshot({
        path: join(outDir, `${shot.name}-${viewport.name}-${theme}.png`),
        fullPage: true,
      });
      const width = await page.evaluate("document.documentElement.scrollWidth");
      const inner = await page.evaluate("window.innerWidth");
      if (width > inner + 1) {
        console.error(`horizontal overflow on ${shot.name} ${viewport.name} ${theme}: ${width} > ${inner}`);
      }
      violations.push(...(await page.evaluate("window.__shotCsp ?? []")).map((v) => `${shot.name}/${viewport.name}/${theme}: ${v}`));
    }
    await context.close();
  }
}
await browser.close();
server.close();

if (violations.length > 0) {
  console.error("CSP violations while shooting:");
  for (const violation of violations) console.error(` - ${violation}`);
  process.exitCode = 1;
} else {
  console.log(`screenshots written to design/screenshots/${label}`);
}
