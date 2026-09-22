# Roadmap

Each step ends with something that can be checked. Nothing moves on while the
previous step's checks are red.

## Step 0: framing ✅ done

- Name, license, repository, v1 scope.
- Architecture, security model, ADRs 0001–0008 (0009 and 0010 added before step 2).
- HTTP API contract `api/openapi.yaml` (v1).

## Step 1: server foundation ✅ done

- `server/` package, uv, ruff, pyright strict, pytest, import-linter.
- Settings (environment + database, secrets encrypted with AES-GCM), structured logs
  with redaction.
- SQLAlchemy Core + Alembic, SQLite WAL, backup before a migration.
- `/healthz` and `/api/v1/server/info`, problem-details error handler.
- Dockerfile (non-root, read-only rootfs), CI: lint, types, tests, audit, image build.

**Check:** CI green. The image starts, `server/info` answers, and a contract test
passes on the implemented endpoints.

## Step 2: setup, authentication and web console shell

Server:

- Setup code, claim (setup session), media server configuration.
- Jellyfin / Emby password, Plex PIN and Jellyfin Quick Connect sign-in, for the app
  (token pair) and the console (cookie).
- JWT + rotating refresh tokens with strict reuse detection, sessions per device,
  web sessions with CSRF token and `Origin` check, roles re-synced at every sign-in
  (ADR 0010), rate limits.
- Phone pairing: create, poll and revoke from the console, preview and exchange for
  the app. `public_url` setting.
- Admin: settings with masked secrets, users (promote, demote, disable, limits).
- Serving the console's static files with its Content-Security-Policy.

Web console (`web/`, [ADR 0009](adr/0009-web-console-and-phone-pairing.md)):

- React + Vite + TypeScript strict, ESLint, Vitest, generated API client,
  `shared/i18n` (en, fr), built into the server image.
- Pages: setup wizard (claim, media server, `public_url`), sign-in (all three
  methods), server settings, users, "Connect a phone", my sessions.

**Check:** a security test suite covering the claim without a code, refresh-token
reuse, cross-user access, CSRF (missing token, wrong `Origin`), bearer tokens refused
on console endpoints, admin role re-sync and the last-admin rule, pairing (expiry,
single use, brute-force limit), lockout and redaction. In a browser, the console
claims a fresh server and signs in with each method against real Jellyfin, Emby and
Plex test instances in containers; pairing is exercised through the API.

## Step 3: adapters

- TMDb, OMDb.
- Media servers: engagement, library, deep links (Jellyfin, Emby, Plex).
- Request backend: Seerr family, requests on behalf of the matching user.
- AI providers: OpenAI, Anthropic, Gemini, Mistral, OpenAI-compatible, Ollama, each
  with a model list and error mapping.
- Console: connector pages with connection tests, AI provider and model picker.

**Check:** contract tests per adapter (recorded responses), plus one live run per AI
provider with a small schema. Every connector can be configured and tested from the
console.

## Step 4: swipe engine

- Port of the fork's engine and its tests: batches, calibration, novelty, mood,
  balancing, enrichment, translation, profile, likes (`like` votes only), stats,
  reset.
- `skip` vote (60-day cool-down, ignored by the profile, the prompt and the stats).
- Region's streaming providers (TMDb, cached), users' streaming services, the
  `subscribed` flag on card providers.
- Stored batches and cards, persisted jobs, warm-up, per-user daily cap and
  concurrency.
- Console: AI usage page.

**Check:** the ported tests pass. A real batch is generated end to end through the API
with each AI provider family. `/status` stays under 50 ms during a generation.

## Step 5: packaging and first deployment

- Signed multi-arch image on GHCR (server + built console), SBOM, documented
  `docker-compose.yml` and reverse-proxy notes (HTTPS for the console).
- `tindeerr import suggestarr`.
- Deployment on the author's homelab next to the fork, which is left untouched.
  Setup and configuration are done in the web console.

**Check:** the server is claimed and configured from the console only. The author's
votes and profile are imported. Monitoring is in place (Uptime Kuma).

## Step 6: app foundation

- Clickable mockups of the main screens, validated before any screen is built.
- Expo + TypeScript strict, ESLint, Jest, i18n (en, fr), theme (dark first), generated
  API client.
- Connect flow: scan a pairing QR code (confirmation screen with server URL and user
  name), or server URL → `server/info` → sign-in (password / Plex PIN / Quick
  Connect). A server with `setup_required` gets a message to open its console.
- Secure token storage, single-flight token refresh, compatibility checks.

**Check:** sign-in and QR pairing work on a real phone against the deployed server,
for all three media server types. A unit test shows that concurrent requests with an
expired token cause exactly one refresh.

## Step 7: the deck

- Card stack with the four gestures (right like, left dislike, up "seen, liked", down
  "seen, not for me"), buttons with the same actions, a "Not now" button for `skip`
  (the four swipe directions are taken), haptics, undo of the last vote (skip
  included).
- Media type and novelty switches, mood, calibration progress, badges (pick type,
  providers with "on your services", ratings), trailer.
- Like → request dialog, or direct request when enabled.

**Check:** the same scenarios as the web version, as Maestro flows. A smooth 60 fps
swipe on a mid-range phone.

## Step 8: likes, taste, settings

- "My likes" (to request / requested, `like` votes only), taste profile (bullets,
  edit, refresh), stats.
- Preferences (including "my streaming services"), sessions, sign-out, data reset.
- No admin screens: admin work is in the web console. At most a read-only server
  status.

**Check:** feature parity with the fork's `swipe.3` on the user side; admin parity
is covered by the console.

## Step 9: mobile extras

- Offline vote queue with idempotent sync.
- Notifications: "new batch ready", "your request is available". The relay is chosen
  at this step: Expo push service (simple, content kept minimal) or UnifiedPush / ntfy
  (no Google dependency). Possibly both.
- "Watch on Jellyfin / Emby / Plex" once a title is available.
- Accessibility pass (TalkBack, font scaling, contrast).

**Check:** airplane mode then back online loses no vote and doubles none.

## Step 10: beta

- End-to-end Maestro suite in CI on an emulator, crash-free sessions measured by
  testers.
- Closed beta: signed APK on GitHub Releases (+ Obtainium), and a Google Play closed
  test. New personal Play accounts must run a closed test with testers for two weeks
  before going to production; the exact rules are checked at that time.

## Step 11: release

- Google Play production, GitHub Releases, documentation site.
- Decide on the iOS port (Apple Developer account, EAS Build, APNs).

## Open questions

- **Trademark risk of the name.** "Tindeerr" plays on a registered trademark. The
  Play Store may reject the listing or receive a complaint. The name only lives in a
  few constants and the Android `applicationId` is neutral, so a rename stays cheap
  until the store listing exists.
- **Push relay (step 9).**
- **Later sources:** Trakt history, Radarr/Sonarr direct requests, OIDC sign-in.
