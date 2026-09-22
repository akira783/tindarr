# Roadmap

Each step ends with something that can be checked. Nothing moves on while the
previous step's checks are red.

## Step 0: framing ✅ in review

- Name, license, repository, v1 scope.
- Architecture, security model, ADRs 0001–0008.
- HTTP API contract `api/openapi.yaml` (v1).

## Step 1: server foundation

- `server/` package, uv, ruff, pyright strict, pytest, import-linter.
- Settings (environment + database, secrets encrypted with AES-GCM), structured logs
  with redaction.
- SQLAlchemy Core + Alembic, SQLite WAL, backup before a migration.
- `/healthz` and `/api/v1/server/info`, problem-details error handler.
- Dockerfile (non-root, read-only rootfs), CI: lint, types, tests, audit, image build.

**Check:** CI green. The image starts, `server/info` answers, and a contract test
passes on the implemented endpoints.

## Step 2: setup and authentication

- Setup code, claim, media server configuration.
- Jellyfin / Emby password sign-in, Plex PIN sign-in.
- JWT + rotating refresh tokens, sessions per device, roles, rate limits.
- Admin: settings with masked secrets, connector tests, users.

**Check:** a security test suite covering the claim without a code, token reuse,
cross-user access, lockout and redaction. Tested against real Jellyfin, Emby and Plex
test instances in containers.

## Step 3: adapters

- TMDb, OMDb.
- Media servers: engagement, library, deep links (Jellyfin, Emby, Plex).
- Request backend: Seerr family, requests on behalf of the matching user.
- AI providers: OpenAI, Anthropic, Gemini, Mistral, OpenAI-compatible, Ollama, each
  with a model list and error mapping.

**Check:** contract tests per adapter (recorded responses), plus one live run per AI
provider with a small schema.

## Step 4: swipe engine

- Port of the fork's engine and its tests: batches, calibration, novelty, mood,
  balancing, enrichment, translation, profile, likes, stats, reset.
- Stored batches and cards, persisted jobs, warm-up, per-user daily cap and
  concurrency.

**Check:** the ported tests pass. A real batch is generated end to end through the API
with each AI provider family. `/status` stays under 50 ms during a generation.

## Step 5: packaging and first deployment

- Signed multi-arch image on GHCR, SBOM, documented `docker-compose.yml`.
- `tindeerr import suggestarr`.
- Deployment on the author's homelab next to the fork, which is left untouched.

**Check:** the author's votes and profile are imported. Monitoring is in place (Uptime
Kuma).

## Step 6: app foundation

- Clickable mockups of the main screens, validated before any screen is built.
- Expo + TypeScript strict, ESLint, Jest, i18n (en, fr), theme (dark first), generated
  API client.
- Connect flow: server URL → `server/info` → setup (claim) or sign-in (password /
  Plex).
- Secure token storage, automatic refresh, compatibility checks.

**Check:** sign-in works on a real phone against the deployed server, for all three
media server types.

## Step 7: the deck

- Card stack with the four gestures (right like, left dislike, up "seen, liked", down
  "seen, not for me"), buttons with the same actions, haptics, undo of the last vote.
- Media type and novelty switches, mood, calibration progress, badges (pick type,
  providers, ratings), trailer.
- Like → request dialog, or direct request when enabled.

**Check:** the same scenarios as the web version, as Maestro flows. A smooth 60 fps
swipe on a mid-range phone.

## Step 8: likes, taste, settings, admin

- "My likes" (to request / requested), taste profile (bullets, edit, refresh), stats.
- Preferences, sessions, sign-out, data reset.
- Admin screens: connectors, AI provider and model picker, users, usage.

**Check:** feature parity with the fork's `swipe.3`, plus the admin screens.

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
