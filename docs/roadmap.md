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

## Step 3: adapters ✅ done, except one live check and one decision

- TMDb (search with the year retry, details with the English fallback, watch providers
  by region, trailers, the content filters) and OMDb (IMDb, Rotten Tomatoes,
  Metacritic), behind the `Metadata` and `RatingsSource` ports.
- Media servers: `library_ids`, `engagement` and `deep_link` for Jellyfin, Emby and
  Plex. Engagement keeps the fork's measured rules — 60 % of a series' episodes is
  "watched", 90 % of a film's runtime is too, and a play count is never a signal.
  Listings are paged, which the fork does not do.
- **Decision: Plex tokens — [ADR 0012](adr/0012-plex-tokens.md), *proposed*, waiting on
  the owner.** The adapter was written on what the stored account token can already do:
  the owner's own progress in full, everybody else's reconstructed from the server's
  history, which records completed viewings and no offsets. The ADR lays out keeping the
  account token (per-user progress and the user sync, but a leak reaches a whole plex.tv
  account) against a server-scoped one (a leak reaches one server, but the user sync
  needs another source), and recommends the first with three conditions. Nothing is
  implemented either way until the owner decides.
- Request backend: Seerr v3.x (Jellyseerr as legacy, Overseerr for Plex only),
  requests on behalf of the matching user (`X-API-User`), users matched by listing them
  without `X-API-User`, ids normalised.
- AI providers: OpenAI, Anthropic, Gemini, Mistral, OpenAI-compatible, Ollama, each
  with a model list, structured output, one validation retry and the five error codes.
- Admin: `PUT`/`DELETE /admin/connectors/{kind}` and `.../test` for the four optional
  connectors, `POST /admin/llm/models`. A stored secret is only reused for the same
  address, where "the same address" counts the AI provider and the request backend's
  `verify_tls` as part of it.
- Console: a connectors page with a card per connector, connection tests, and an AI
  provider with a model picker fed by the provider itself.

**Check.** (a) is done; (b) needs the owner's own API keys and is not run here.

- **(a) Contract tests per adapter (pytest, no network).** Fake TMDb, OMDb, Seerr,
  OpenAI, Anthropic, Gemini and Ollama services on `httpx.MockTransport`, with the
  **real** SDKs and adapters driven against them, covering the failure paths a service
  one does not control produces: `401`, `429`, `5xx`, malformed JSON, a body that is not
  JSON at all, and answers of an unexpected shape. Every connector can be configured and
  tested from the console, and the console's own tests drive that page.
- **(b) One live run per AI provider with a small schema.** Not run: it needs the
  owner's OpenAI, Anthropic, Gemini and Mistral keys and it costs money on each. What it
  would prove that the contract tests cannot: that each provider really honours the
  schema that was sent, and that the parameter ladder lands on a rung the real endpoint
  accepts. Ollama can be checked locally with no key.

## Step 4: swipe engine

Shaped by [ADR 0013](adr/0013-recommendation-engine.md): TMDb retrieves the candidates,
the model picks and explains, and nothing ships without the harness saying it is better.

**4.1 Evaluation harness (first, before the engine).** ✅ done —
[the reference](evaluation.md).

- Replay a set of real votes against any candidate strategy, offline, without paying for
  a generation: cards produced per batch, share of already-seen titles, agreement with the
  votes that were cast, diversity within a batch.
- A fixture set of votes that can live in the repository (anonymised), plus the ability to
  point the harness at a real instance's database.

What shipped: the `Strategy` port (`propose(context, size)`, where the context is the
whole input and holds only the votes cast before the batch), two baselines to measure
against, the replay and its metrics, `tindarr eval run / fixtures / import`, a committed
generated vote set carrying the distribution ADR 0013 measured, recorded TMDb answers so
a run costs nothing, and a committed baseline per strategy that `server.yml` fails on.
The committed fixture is **generated**, not a real person's votes; `tindarr eval import`
builds a private one from a real database into a path the repository ignores.

**4.2 Retrieval.** ✅ done — `server/src/tindarr/swipe/retrieval.py`,
[the reference](evaluation.md#the-candidate-pool).

- Candidate pool from TMDb: `/{kind}/{id}/recommendations` for the user's recent likes,
  plus `/discover/{kind}` filtered by genre, era, rating and original language, under an
  **adaptive popularity floor** driven by the novelty setting — and a fame ceiling in
  vote counts at the bold end, which is the half of the band that attacks ADR 0013's
  47 %.
- Exclusions applied to the pool, not after the model: voted titles, the library, cards
  already served, content filters, the wrong media type.
- The model receives the pool and returns an ordered selection with a rationale per card;
  its output is validated against a schema, and a title it did not get from the pool is
  dropped.
- The harness measures it offline through recorded TMDb and model answers, and **all
  three strategies now draw from this pool**, so the floors answer the same question the
  candidate does.

**4.3 What carries over from the fork** (behaviour and tests): batches, calibration,
novelty levels, mood, safe/explore balancing, enrichment (translation, providers,
ratings, trailer), the taste profile (bullets, user edits kept), likes (`like` votes
only), stats, reset. Plus the `skip` vote (60-day cool-down, ignored by the profile, the
prompt and the stats).

The batch prompt, calibration, novelty, mood, the safe/explore split and the JSON repair
with its single validation retry are done (`server/src/tindarr/swipe/hybrid.py`; the
repair and the retry live in the provider adapters since step 3). What is left for 4.5
and beyond: enrichment, the taste profile's own prompt and refresh, likes, stats, reset
and the skip cool-down — all of which need stored batches and votes.

**4.4 Knowing what the user has already seen.** ✅ done, except the request-queue badge,
which needs the stored cards of 4.5 —
[the architecture](architecture.md#what-the-household-has-already-watched-from-outside-the-deck).

- The calibration grid: a wall of famous posters to tick, which conveys years of watching
  in minutes. Ranked by TMDb vote count rather than by this week's popularity, spread
  across five decades and across what is famous in the household's own language, capped
  so no genre takes a third of the wall, and never asking twice — a poster answered "no"
  is answered.
- File imports as **taste** sources, not filters ([ADR 0013](adr/0013-recommendation-engine.md)
  measured them at 6 % of the already-seen problem): Netflix viewing history (both
  exports), IMDb ratings, Letterboxd exports. Parsing splits on `": "` but never on a
  French `" : "` — in any whitespace form, because French typography writes that space as
  U+00A0 and the prototype's plain-space rule silently never fired on the rows that
  needed it — retries on the left-hand side, treats `&` as `et`, ranks by similarity then
  popularity, and **abstains into a review queue rather than guessing**: `results[0]` is
  the root cause of the false matches in every comparable project. An IMDb `tt…` id is
  resolved exactly through `/find` and never searched for.
- Episodes watched per series, counted against the total on TMDb, give "finished / in
  progress / sampled and dropped" for everything watched outside the media server, with
  the media server port's own thresholds and no others.
- Neither an import nor a grid tick is a **vote**: they are excluded from the candidate
  pool and read as engagement, and no statistic counts them.
- A title already in the user's request queue is **shown as such on the card**, not
  filtered out. *(4.5: it needs a stored card to be shown on.)*

**4.5 Serving it.** ✅ done — the engine is reachable and durable.

- **Stored batches and cards** ([ADR 0007](adr/0007-server-side-cards.md)): a card is a
  row with an opaque id, and a vote sends that id and one of five words. Everything else
  — the title, the year, the pick type — is copied off this server's own row, so the
  statistics are not a number the client writes. A card leaves the deck 24 h after being
  served, a vote on it is accepted for a month (the offline queue), and only the unvoted
  ones are ever purged.
- **Background work with a state that outlives the process** (`jobs`): the deck answers
  `202` while a batch is built, one generation per user at a time, a failure reported
  once, and a job a restart left behind closed at startup so it cannot hold a slot for
  ever. Warm-up 30 s after startup then every 6 h for users seen in the last 14 days;
  the daily purge.
- **The endpoints of the contract**: deck, votes (batch submit, idempotent per
  `client_vote_id`, undo, reset), requests, likes, taste profile, preferences, providers,
  stats — plus `GET /swipe/status`.
- **Enrichment**: translation, the region's providers with the `subscribed` flag from the
  user's own services, ratings, trailer, and the availability badge. The last two are
  computed when the card is served, so ticking a service updates cards already in
  somebody's hand.
- **The taste profile**: Loves / Avoids / Nuances, rewritten in the background from votes
  and engagement. What the user wrote lives in its own column, so "a rewrite never
  contradicts it" is a property of the schema rather than a line in a prompt.
- **The per-user daily cap**, charged inside the transaction that claims the job, so two
  phones cannot both see "one left"; and **not refunded on failure**, because "it failed"
  is the state a retry loop is in.
- Console: the AI usage page. *(The import and calibration screens shipped with 4.4.)*

Three things moved in the contract, all of them because the implementation found them
wrong rather than because it wanted room:

- `GET /swipe/deck` gains a documented **`503`**. Its `502` lists the AI provider's
  failures, which is the case that almost never reaches it — a model that fails costs
  the rationales and not the batch, because the retrieved pool is served in its own
  order. What does stop a generation is TMDb refusing the genre list a content filter
  needs, and that is a `metadata_unreachable`.
- `GET /swipe/providers` answered **`503`** where the contract said `502`; the shared
  `metadata_unreachable` has been a `503` since step 3, so it was the contract that was
  out of step.
- `Deck` gains **`exhausted`**. A blank deck can mean "no cards yet" or "the pool had
  nothing", and a client that cannot tell them apart shows the same spinner for both.

Everything else the contract already said is what shipped.

**4.6 A swipe page in the web console.** ← next. Decided on 2026-09-24, outside the original
plan, which kept swiping for the app (steps 6–7). The console gets a deck: the four
verdicts by keyboard and mouse, a "not now" button, provider and rating badges, the
trailer, and the request dialog on a like. No touch gestures, no notifications — those
stay with the app. Two reasons: the owner can swipe from a browser weeks before an APK
exists, and every session grows the vote set the engine is judged on, which is the
shortage ADR 0013's measurements ran into.

**Check.**

- ✅ The harness runs in CI on the fixture votes and prints its metrics; a strategy change
  that makes them worse fails the build.
- ✅ The ported tests pass.
- A real batch is generated end to end through the API with each AI provider family, and
  every card in it comes from the retrieved pool. **Done for the OpenAI-compatible
  family** (2026-09-24), against real TMDb and the author's local ChatMock: two batches
  of ten in French, 17 s and 29 s to the first card, 6 094 input and 2 232 output tokens
  over four generations (two batches and two profile rewrites), **no title repeated
  between the two batches**, providers and trailers on every card, and a taste profile
  written from the ten answers. The other four families need the owner's paid keys and
  are the same check as step 3(b).
- On the fixture votes, the share of already-seen cards is materially below the 47 %
  measured on the fork.
- ✅ Importing a Netflix history file produces the expected split of finished,
  in-progress and dropped series, and every uncertain match lands in the review queue
  rather than in the profile. Measured on the author's real five-month export: 72 titles,
  1 trailer dropped, **71 identified and 1 queued**; 27 films watched and 44 series split
  into 7 finished, 3 mostly watched, 3 in progress, 12 paused, 13 sampled and dropped,
  and 6 seen with no extent claimed.
- ✅ `/status` answers while a generation is in flight: it reads rows and calls no
  service, and the generation runs in its own task.

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
