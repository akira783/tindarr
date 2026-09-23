# Roadmap

Each step ends with something that can be checked. Nothing moves on while the
previous step's checks are red.

## Step 0: framing ✅ done

- Name, license, repository, v1 scope.
- Architecture, security model, ADRs 0001–0008 (0009 to 0011 added before step 2,
  0011 after a four-part review of the design and the step-1 code).
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

## Step 2: setup, authentication and web console shell ✅ done

Everything here is specified in [the authentication reference](auth.md) and
[`api/openapi.yaml`](../api/openapi.yaml); the console and build parts in the
[architecture](architecture.md#web-console).

Server:

- Request context: trusted proxies (rightmost untrusted hop), effective scheme,
  allowed hosts, origin, private-network test.
- Migration `0002`: `users`, `sessions`, `refresh_tokens`, `pairings`, new
  `server_state` columns; new settings (`media_server_verify_tls`, `public_url`,
  `password_sign_in`, and the general settings stored for later steps).
- Setup: setup code (path only in the logs), claim with a single active setup session,
  `GET /setup/state`, `PUT /setup/media-server` (with `setting_locked` for
  environment-set fields), completion bound to the setup cookie with a new web session,
  `tindarr media-server reset` CLI.
- Media server part of the ports, used by auth: `MediaServer.identify`, `test`,
  `authenticate_password`, `quick_connect_start`, `quick_connect_poll`, `list_users` for
  Jellyfin and Emby (the `Authorization: MediaBrowser …` header, Jellyfin 10.10+), and
  the `PlexTv` client (PINs, account, resources, device deletion, shared users). The
  library and engagement parts stay in step 3.
- Sign-in for the app (token pair) and the console (cookie): password (with the
  per-username cap and `password_sign_in`), Plex PIN (machine identifier, `owned`,
  device deletion), Quick Connect (with the cleanup sweep); remote-access rule;
  handles bound to purpose and initiator, with caps; the read-only PIN status for
  setup.
- Sessions: session and user loaded on every request, absolute lifetimes, JWT (PyJWT,
  HS256, required claims), refresh rotation as a compare-and-set, web sessions with
  CSRF token and `Origin` check, step-up re-authentication, hourly user sync, daily
  purge.
- Pairing: create, approve, reject and revoke from the console; preview, request and
  completion (PKCE, confirmation code) for the app. `public_url` with its verification.
- Rate limits (in memory, per client IP, global slowdowns).
- Admin, in step 2:
  - `GET /admin/settings` and `PATCH /admin/settings`: every field is stored and
    returned; `name`, `public_url` and `password_sign_in` take effect now, the others
    (`language`, `streaming_region`, `daily_generation_limit`, `warm_up_enabled`,
    `content_filters`) are only stored until step 4.
  - `GET /admin/connectors` lists every kind (all but `media_server` as
    `configured: false`); `PUT /admin/connectors/media_server` and its `test` are
    implemented (media server administrator + re-authentication, identity change
    rules); the other kinds answer `404` until step 3.
  - Users: list, promote, demote, disable, limits, revoke sessions. `votes` and
    `generations_today` are `0` until step 4; `request_backend_user_found` is left out
    until step 3.
  - `POST /admin/llm/models` and `GET /admin/usage` come in steps 3 and 4.
- Serving the console: `TINDARR_WEB_DIR`, SPA fallback outside `/api`, path-aware
  security headers.
- Logging: the new redacted key names, no credentials in URLs, security events.

Web console (`web/`, [ADR 0009](adr/0009-web-console-and-phone-pairing.md)):

- React + Vite + TypeScript strict, React Router, TanStack Query, i18next with
  `shared/i18n` (en, fr), ESLint, Vitest; generated API client committed.
- Skeleton: fetch wrapper with `X-CSRF-Token`, auth guard on
  `GET /auth/web/session`, typed problem errors.
- Pages: setup wizard (claim, media server or "locked by the environment", admin
  sign-in, `public_url`), sign-in (the methods in `auth_methods`), server settings,
  users, "Connect a phone" (QR code as SVG or canvas, host shown, approval with the
  confirmation code), my sessions (revoke), delete my data, re-authentication dialog.

Build and CI:

- Image built from the repository root with a Node stage pinned by digest, root
  `.dockerignore`; compose example updated (`context: ..`).
- `web.yml`; `server.yml` also triggered by `web/**` and `shared/**`; `e2e.yml`.

**Check.** (a) runs anywhere. (b) needs Docker and a browser; its Jellyfin and Emby
targets run locally as well as in CI (`.github/scripts/e2e-stack.sh`), while the Plex
target runs in CI only, on a dispatch, because it needs a real plex.tv account.

- **(a) Security test suite (pytest, runs locally and in `server.yml`).** Fake media
  servers and a fake plex.tv built on `httpx.MockTransport`, no network. It covers:
  - request context: forwarded headers from untrusted peers ignored, rightmost
    untrusted hop, garbage and IPv6 entries, `host_not_allowed`, origin building;
  - setup: claim without or with a wrong code, second claim revoking the first,
    completion refused without the setup cookie or for a non-administrator, new session
    at completion, code never in the logs, `setting_locked`, restart keeps the code;
  - sign-in: identical errors for unknown user and wrong password, the per-username
    cap (never more than 2 failures reach the fake server in 15 minutes), per-IP pause,
    `password_sign_in` modes, remote-access rule at sign-in and on later requests,
    Plex resource matching by `machineIdentifier` only, `owned` for admin, device
    deletion (and its failure path), Quick Connect cleanup of abandoned approvals;
  - handles: wrong purpose, wrong initiator (cookie, verifier, session), expiry,
    single use, per-IP and global caps, owner-token PIN surviving a failed connection
    test;
  - tokens: JWT with a wrong `alg`, `typ`, `aud`, `iss`, `kid`, or expired beyond the
    leeway; refresh-token reuse; two concurrent rotations of the same token (exactly one
    wins); revocation and disabling effective on the next request; absolute lifetimes;
  - CSRF: missing or wrong token, missing or wrong `Origin`, bearer on console
    endpoints (`401`), setup session on `/me` (`401`), cookie ignored when a bearer
    header is present;
  - roles: admin re-sync at sign-in, the hourly sync (clears, never sets), last-admin
    rule, promoted admin refused on the media server connector and `public_url`,
    re-authentication required and expiring, identity change revoking every session and
    unlinking users, unexpected identity stopping sign-ins;
  - pairing: expiry, single use, preview revealing nothing for invalid codes, no tokens
    before approval, rejection, wrong verifier, rate limits;
  - `public_url`: proof checked, redirects not followed, nonce not answered when not
    pending;
  - rate limits: every `429` carries `retry_after_ms` and `Retry-After`; global
    thresholds slow down without refusing;
  - redaction of every new secret kind, security headers per path, SPA fallback never
    under `/api`, contract validation of every new endpoint's responses.
- **(b) End-to-end workflow (`e2e.yml`, GitHub Actions).** Builds the image, starts it
  next to Jellyfin and Emby containers (pinned by digest) seeded through their
  startup-wizard APIs (`/Startup/…`: admin user, then an API key and a second,
  non-admin user). Three targets run, one Tindarr server each: Jellyfin 12, the oldest
  Jellyfin supported (10.10) and Emby. Playwright (Chromium) drives the console against
  the built image: claim a fresh server, configure the media server, complete setup with
  an administrator password sign-in, set `public_url`, sign a plain user in and check
  they land where a non-administrator belongs, sign in with Quick Connect on Jellyfin
  (the test approves the code through `POST /QuickConnect/Authorize` with a real user
  token), then a full pairing round trip — the console creates the code, the API is
  called as the app would (preview, request with a PKCE challenge, the confirmation
  code matched on both sides, approval in the console, completion) and the token pair is
  used and rotated. Every test fails on any `securitypolicyviolation`, any console
  message about the CSP and any uncaught exception, and the run fails if a Tindarr
  server logged an error. Plex runs only on `workflow_dispatch`, with repository secrets
  (a Plex account token; a claim token is minted from it, since claim tokens expire in
  minutes): the test approves the PIN through plex.tv's device-link endpoint with that
  token. Pull requests from forks never get these secrets. The same workflow lints the
  workflows (`actionlint`) and the CI shell scripts (`shellcheck`).

## Step 3: adapters

- TMDb, OMDb.
- Media servers: engagement, library, deep links (Jellyfin, Emby, Plex).
- **Decision: Plex tokens.** Step 2 stores the owner's account-wide token. Per-user
  watch progress needs each user's server token (`shared_servers` for friends, Home
  user switching for Home users). Choose between keeping the account-wide token (it can
  do anything on the owner's plex.tv account, but gives the user sync and the per-user
  tokens) and storing only server-scoped tokens (a leak only reaches that server, but
  the user sync and per-user progress need another source). Record it in an ADR.
- Request backend: Seerr v3.x (Jellyseerr as legacy, Overseerr for Plex only),
  requests on behalf of the matching user (`X-API-User`), users matched by listing them
  without `X-API-User`, ids normalised.
- AI providers: OpenAI, Anthropic, Gemini, Mistral, OpenAI-compatible, Ollama, each
  with a model list and error mapping.
- Console: connector pages with connection tests, AI provider and model picker.

**Check:** contract tests per adapter (recorded responses), plus one live run per AI
provider with a small schema. Every connector can be configured and tested from the
console.

## Step 4: swipe engine

Shaped by [ADR 0013](adr/0013-recommendation-engine.md): TMDb retrieves the candidates,
the model picks and explains, and nothing ships without the harness saying it is better.

**4.1 Evaluation harness (first, before the engine).**

- Replay a set of real votes against any candidate strategy, offline, without paying for
  a generation: cards produced per batch, share of already-seen titles, agreement with the
  votes that were cast, diversity within a batch.
- A fixture set of votes that can live in the repository (anonymised), plus the ability to
  point the harness at a real instance's database.

**4.2 Retrieval.**

- Candidate pool from TMDb: titles similar to the user's likes, discovery filtered by
  genre, era, country, rating, original language, and an **adaptive popularity floor**
  driven by the novelty setting.
- Exclusions applied to the pool, not after the model: voted titles, the library, cards
  already served, content filters.
- The model receives the pool and returns an ordered selection with a rationale per card;
  its output is validated, and a title it did not get from the pool is dropped.

**4.3 What carries over from the fork** (behaviour and tests): batches, calibration,
novelty levels, mood, safe/explore balancing, enrichment (translation, providers,
ratings, trailer), the taste profile (bullets, user edits kept), likes (`like` votes
only), stats, reset. Plus the `skip` vote (60-day cool-down, ignored by the profile, the
prompt and the stats).

**4.4 Knowing what the user has already seen.**

- The calibration grid: a wall of famous posters to tick, which conveys years of watching
  in minutes.
- File imports as **taste** sources, not filters ([ADR 0013](adr/0013-recommendation-engine.md)
  measured them at 6 % of the already-seen problem): Netflix viewing history, IMDb ratings,
  Letterboxd exports. Parsing splits on `": "` but never on a French `" : "`, retries on the
  left-hand side, treats `&` as `et`, ranks by similarity × popularity, and **abstains into
  a review queue rather than guessing** — `results[0]` is the root cause of the false
  matches in every comparable project.
- Episodes watched per series, counted against the total on TMDb, give "finished / in
  progress / sampled and dropped" for everything watched outside the media server.
- A title already in the user's request queue is **shown as such on the card**, not
  filtered out.

**4.5 Serving it.** Stored batches and cards, persisted jobs, warm-up, per-user daily cap
and concurrency, the region's streaming providers (TMDb, cached), the user's own services
and the `subscribed` flag on card providers. Console: AI usage page, and the import screen.

**Check.**

- The harness runs in CI on the fixture votes and prints its metrics; a strategy change
  that makes them worse fails the build.
- The ported tests pass.
- A real batch is generated end to end through the API with each AI provider family, and
  every card in it comes from the retrieved pool.
- On the fixture votes, the share of already-seen cards is materially below the 47 %
  measured on the fork.
- Importing a Netflix history file produces the expected split of finished, in-progress
  and dropped series, and every uncertain match lands in the review queue rather than in
  the profile.
- `/status` stays under 50 ms during a generation.

## Step 5: packaging and first deployment

- Signed multi-arch image on GHCR (server + built console), SBOM, documented
  `docker-compose.yml` and reverse-proxy notes (HTTPS for the console,
  `TINDARR_TRUSTED_PROXIES`, passing `Host`, `TINDARR_PUBLIC_URL`).
- `tindarr import suggestarr`.
- Deployment on the author's homelab next to the fork, which is left untouched.
  Setup and configuration are done in the web console.

**Check:** the server is claimed and configured from the console only. The author's
votes and profile are imported. Monitoring is in place (Uptime Kuma).

## Step 6: app foundation

- Clickable mockups of the main screens, validated before any screen is built.
- Expo + TypeScript strict, ESLint, Jest, i18n (en, fr), theme (dark first), generated
  API client.
- Connect flow: scan a pairing QR code (confirmation screen with the host shown
  prominently in punycode and the user name, refusal of `http` for non-private hosts,
  then the confirmation code while waiting for the console approval), or server URL →
  `server/info` → sign-in (password / Plex PIN / Quick Connect, with a PKCE verifier
  for the handles). A server with `setup_required` gets a message to open its console.
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

**Check:** the same scenarios as the fork's web UI, as Maestro flows. A smooth 60 fps
swipe on a mid-range phone.

## Step 8: likes, taste, settings

- "My likes" (to request / requested, `like` votes only), taste profile (bullets,
  edit, refresh), stats.
- Preferences (including "my streaming services"), sessions (list; revoking other
  sessions and deleting the account are done in the console), sign-out, votes reset.
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

- **Trademark risk of the name.** "Tindarr" plays on a registered trademark. The
  Play Store may reject the listing or receive a complaint. The name only lives in a
  few constants and the Android `applicationId` is neutral, so a rename stays cheap
  until the store listing exists.
- **Push relay (step 9).**
- **Later sources:** Trakt history, Radarr/Sonarr direct requests, OIDC sign-in.
- **Relinking users after a media server change.** Unlinked accounts keep their data
  but cannot sign in; merging them into new accounts is not in v1.
