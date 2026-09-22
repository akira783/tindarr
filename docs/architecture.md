# Architecture

## Overview

```
 Android app (Expo)                     Tindeerr server (one per household)
┌─────────────────────┐  HTTPS, JSON   ┌──────────────────────────────────────┐
│ Deck · Likes ·      │  /api/v1       │  HTTP API (FastAPI)                   │
│ Taste · Settings ·  │ ─────────────▶ │   auth · pairing · swipe · admin     │
│ Sessions            │ ◀───────────── │  Web console (static files, /)        │
│ offline vote queue  │  bearer token  │  Domain (swipe engine)                │
└─────────────────────┘                │   batches · votes · taste profile    │
 Browser: web console (React)          │  Jobs (asyncio, state in DB)          │
┌─────────────────────┐  same origin   │                                      │
│ Setup · Connectors ·│ ─────────────▶ │                                      │
│ Settings · Users ·  │ ◀───────────── │                                      │
│ Usage · Pair phone  │ cookie + CSRF  │                                      │
└─────────────────────┘                │                                      │
                                       │   batch generation · profile rewrite │
                                       │   warm-up · translation cache        │
                                       │  Adapters                             │
                                       │   MediaServer   Jellyfin/Emby/Plex   │
                                       │   RequestBackend Seerr family         │
                                       │   Metadata      TMDb, OMDb            │
                                       │   LlmProvider   OpenAI, Anthropic,   │
                                       │                 Gemini, Mistral,     │
                                       │                 OpenAI-compatible,   │
                                       │                 Ollama               │
                                       │  Storage: SQLite (WAL) + migrations  │
                                       └──────────────────────────────────────┘
```

Every household runs its own server ([ADR 0001](adr/0001-self-hosted-server-and-mobile-app.md)).
The app only knows the server URL the user typed (or scanned) and the tokens that
server issued. There is no central Tindeerr service.

The server has two clients ([ADR 0009](adr/0009-web-console-and-phone-pairing.md)):

- **The web console,** served by the server itself, for setup and all admin work,
  and for pairing phones. It is used from a computer, rarely.
- **The mobile app,** for daily use: the deck, likes, taste profile, preferences
  and sessions.

## Server

### Layers

| Layer | Package | Depends on | Rule |
|---|---|---|---|
| Main | `tindeerr.main` | everything | Composition root: builds the adapters, hands them to the domain as ports, starts the jobs. |
| API | `tindeerr.api` | domain, auth, storage, ports, core | HTTP only: request context (client IP, scheme, allowed hosts), cookies, CSRF and `Origin` checks, parsing, status codes, problem details, security headers, serving the console's static files. No business logic. |
| Jobs | `tindeerr.jobs` | domain, auth, storage | Background work with persisted state. |
| Auth | `tindeerr.auth` | ports (media server, plex.tv), storage, core | Pure functions and services: sessions (mobile, web, setup), tokens, handles, pairing, roles, rate-limit decisions, user sync. No Starlette or FastAPI import (import-linter enforces it): it never sees a request, a cookie or a header, only values the API layer extracted. |
| Domain | `tindeerr.swipe` | ports (interfaces), storage | Swipe engine, prompts, scoring. No HTTP, no vendor SDK. |
| Adapters | `tindeerr.adapters.*` | ports, vendor SDKs, httpx | One package per external system. |
| Ports | `tindeerr.ports` | nothing | `Protocol` classes the domain and auth talk to. |
| Storage | `tindeerr.storage` | SQLAlchemy Core | Tables, repositories, Alembic migrations. |
| Core | `tindeerr.core` | nothing | Bootstrap configuration, key derivation, encryption, logging, and `ProblemError(status, code, detail)`, which auth and storage raise and one API handler renders. Usable by every layer. |

Imports only point downwards. Only `tindeerr.main` imports adapters. CI enforces this
with `import-linter`.

### Ports

```python
class MediaServer(Protocol):
    kind: Literal["jellyfin", "emby", "plex"]
    # Step 2: identity, sign-in and users.
    async def identify(self) -> ServerIdentity                       # server id, product, version
    async def test(self) -> ConnectorStatus                          # coarse result, never a body
    async def authenticate_password(self, username: str, password: str) -> MediaUser  # Jellyfin / Emby
    async def quick_connect_start(self) -> QuickConnectStart         # Jellyfin: code + secret
    async def quick_connect_poll(self, secret: str) -> MediaUser | None  # None while not approved
    async def list_users(self) -> list[MediaUser]                    # hourly sync
    # Step 3: what the swipe engine reads.
    async def library_ids(self) -> LibraryIndex                      # TMDb ids already owned
    async def engagement(self, user: MediaUser) -> list[Engagement]  # watched / abandoned / in progress
    def deep_link(self, item: LibraryItem) -> str | None              # "open in Jellyfin"

class PlexTv(Protocol):            # step 2; used by auth and by the Plex adapter
    async def create_pin(self, client_id: str) -> PlexPin
    async def check_pin(self, pin: PlexPin) -> str | None            # the token once approved
    async def account(self, token: str) -> PlexAccount               # plex.tv account id, name
    async def resources(self, token: str) -> list[PlexResource]      # clientIdentifier, owned
    async def delete_device(self, token: str, client_id: str) -> bool  # unofficial, best effort
    async def shared_users(self, owner_token: str, machine_id: str) -> list[PlexAccount]

class RequestBackend(Protocol):
    async def find_user(self, media_user: MediaUser) -> BackendUser | None
    async def request(self, title: TitleRef, on_behalf_of: BackendUser) -> RequestResult
    async def status(self, titles: list[TitleRef]) -> dict[TitleRef, Availability]

class Metadata(Protocol):          # TMDb; OMDb is an optional enricher
    async def search(self, title: str, year: int | None, kind: MediaKind) -> list[Title]
    async def details(self, ref: TitleRef, language: str) -> TitleDetails
    async def watch_providers(self, ref: TitleRef, region: str) -> list[Provider]
    async def trailer(self, ref: TitleRef, language: str) -> Trailer | None

class LlmProvider(Protocol):
    kind: str
    capabilities: LlmCapabilities   # json_schema | json_mode | text
    async def list_models(self) -> list[str]
    async def generate(self, prompt: Prompt, schema: type[BaseModel]) -> BaseModel
```

`MediaUser` carries the normalised user id, the name, and the `is_admin`,
`remote_access` and `disabled` flags. Adapters own the media server's wire details
(the `Authorization: MediaBrowser …` header, `X-Plex-Token`); see
[the authentication reference](auth.md#4-sign-in-through-the-media-server).

Adding a media server, request backend or AI provider means adding one adapter
package and its contract tests. The domain does not change
([ADR 0005](adr/0005-adapters-and-ai-providers.md)).

### Swipe engine

This is the feature's logic as it exists in the SuggestArr fork, already tested in real
use:

1. **Signals.** Engagement from the media server (watched, abandoned, in progress,
   "mostly watched" for a series at 60 % of its episodes or more), the library, past votes and the taste profile.
   Media server play counts are never used as a rewatch signal: debrid setups inflate
   them.
2. **Batch prompt.** Mode (`calibration` until 15 votes, then `normal`), novelty
   (`familiar` / `balanced` / `bold`), optional mood. The prompt also lists the
   library and the cards already shown, so the model avoids them in the first place.
3. **Resolution.** Every suggestion is matched on TMDb, then filtered (already voted,
   owned, already shown, content filters). The batch is balanced between safe and
   explore picks.
4. **Enrichment.** Translation into the user's language, streaming providers for the
   region, ratings (OMDb), trailer.
5. **Profile.** Rewritten in the background every N votes, as short "Loves / Avoids /
   Nuances" bullets. Text written by the user is kept and never contradicted.

**Votes.** Five values:

| Vote | Gesture | Meaning | Used by |
|---|---|---|---|
| `like` | swipe right | Not seen, wants it | profile, prompt, stats, likes list |
| `dislike` | swipe left | Not seen, not interested | profile, prompt, stats |
| `seen_liked` | swipe up | Seen, liked | profile, prompt, stats |
| `seen_disliked` | swipe down | Seen, not for me | profile, prompt, stats |
| `skip` | "Not now" button | No opinion yet | nothing |

- **Likes list.** Only `like` votes: it is the "to request" list. `seen_liked`
  titles are already seen, so they are profile signals only and are listed nowhere
  else for now.
- **Skip.** The card leaves the deck. The vote does not count toward calibration,
  does not trigger a profile rewrite, is not in the prompt's vote history, and is
  left out of like/dislike stats (counted apart). The title is excluded from new
  batches for 60 days, then may be offered again. Undo works as for any vote.
- **"On your services".** Each user ticks the streaming services they subscribe to
  (TMDb provider ids, in their preferences), from the list of providers available
  in the server's region (`GET /swipe/providers`, from TMDb, cached 7 days). Each
  provider on a card says whether it is one of the user's (`subscribed`: in the list
  and offered by subscription, free or with ads). It is computed when the card is
  returned, so changing the list updates cards already served.

**Changes from the fork:**

- Batches and cards are stored in the database
  ([ADR 0007](adr/0007-server-side-cards.md)). They survive a restart, and a vote only
  sends `card_id` instead of the whole card.
- The fork only reads engagement and library contents from Jellyfin / Emby. The Plex
  side of the `MediaServer` port is new code, not a port.
- The `skip` vote is new. The likes list without `seen_liked` and the "on your
  services" badge come from the fork.

### Background jobs

These run as asyncio tasks inside the API process. Job state is stored in a `jobs`
table (`pending`, `running`, `done`, `failed`, with the error code), not in memory. On
startup, jobs left `running` by a crash are marked failed. There is no Redis or Celery
at this scale. If a second process ever becomes necessary, the table already serves as
the queue.

- **Batch generation.** Started when a batch is consumed or requested. One per user at
  a time: a request for another filter set waits (`202`) until it is done.
- **Warm-up.** 30 s after startup, then every 6 h, only for users active in the last
  14 days.
- **Profile rewrite.** Debounced, one per user at a time.
- **Per-user limits.** A maximum number of generations per day, set by the admin, on
  top of the one-at-a-time rule above (see [security](security.md)).
- **Media server user sync** (from step 2). 60 s after startup, then hourly: disables
  users removed or disabled on the media server, clears lost admin flags, checks the
  server's identity ([auth reference](auth.md#periodic-sync)).
- **Handle sweep** (from step 2). Every 30 s, in memory: drops expired sign-in handles
  and logs out Quick Connect approvals nobody collected.
- **Purge** (from step 2). Daily: old revoked sessions, refresh tokens and pairings.

### Storage

SQLite in WAL mode by default, one file in the data volume. It is accessed through
SQLAlchemy Core (no ORM), so PostgreSQL remains an option without a rewrite. Alembic
migrations run at startup, and a backup copy is taken first when the schema version
changes.

Main tables: `server_state` (single row: install id, setup state, media server
identity), `settings` (secrets encrypted), `users` (with `media_server_admin` and
`promoted`), `sessions` (`kind`: `mobile`, `web` or `setup`), `refresh_tokens`,
`pairings`, then from step 4 `batches`, `cards`, `votes`, `taste_profiles`,
`preferences` (including the user's streaming services), `jobs`, `llm_usage`,
`translations`, plus a cache of the region's streaming providers. The step-2 columns
are specified in [the authentication reference](auth.md#12-tables).

**Transactions.** SQLite's Python driver does not open transactions for DDL or reads by
itself, so the engine uses SQLAlchemy's documented SQLite recipe: the driver's
autocommit handling is turned off and every transaction starts with an explicit
`BEGIN` (`BEGIN IMMEDIATE` for writes). Migrations, refresh-token rotation, pairing
consumption and setup completion rely on it: they either fully happen or not at all.
Migrations also hold a file lock (`<data>/.migrate.lock`), so two processes never
upgrade at once.

**In memory only:** Plex PIN and Quick Connect handles, rate-limit counters,
`public_url` nonces and short caches. A restart forgets them (the consequences are
listed in the [authentication reference](auth.md#12-tables)).

### Configuration

Two kinds of configuration:

- **Bootstrap configuration** (`ServerConfig`), read from the environment only, before
  the database is opened. It holds what the database cannot, and what a stolen admin
  session must not be able to change.
- **Settings**, stored in the database (secrets encrypted) and edited from the web
  console. Any setting can also come from an environment variable; it is then shown as
  locked in the console and a change is refused (`setting_locked`).

Every variable can instead point to a file with the `_FILE` suffix (Docker secrets);
the file wins when both are set.

| Bootstrap variable | Default | Meaning |
|---|---|---|
| `TINDEERR_DATA_DIR` | `data` (`/data` in the image) | Database, backups, key, setup code. |
| `TINDEERR_SECRET_KEY` | generated into `<data>/secret.key` | Master key material. |
| `TINDEERR_HOST`, `TINDEERR_PORT` | `127.0.0.1` (`0.0.0.0` in the image), `8787` | Listening address. |
| `TINDEERR_LOG_LEVEL` | `INFO` | |
| `TINDEERR_API_DOCS` | `false` | Interactive docs at `/api/docs`. |
| `TINDEERR_DB_BACKUPS_KEEP` | `5` | Pre-migration backups kept. |
| `TINDEERR_TRUSTED_PROXIES` | none | IPs/CIDRs whose `X-Forwarded-For`/`-Proto` are honoured ([rules](auth.md#client-ip-and-scheme)). |
| `TINDEERR_ALLOWED_HOSTS` | none | Extra host names accepted in `Host`, besides IP literals, `localhost` and the host of `public_url` ([rules](auth.md#allowed-hosts-dns-rebinding)). Step 2. |
| `TINDEERR_ALLOW_HTTP_CONSOLE` | `false` | Console over plain HTTP from private client IPs ([rules](auth.md#cookies)). Step 2. |
| `TINDEERR_WEB_DIR` | `/app/web` | Built console. When the directory does not exist (development without a build), the console is not served and `/` answers `404`. Step 2. |

Settings, with their environment variable and where they appear in the contract (the
names differ for historical reasons; this table is the mapping):

| Setting | Environment variable | Contract | Step |
|---|---|---|---|
| `server_name` | `TINDEERR_SERVER_NAME` | `ServerSettings.name`, `ServerInfo.name` | 1 |
| `media_server_kind` | `TINDEERR_MEDIA_SERVER_KIND` | `MediaServerConfigInput.server_type`, `Connector.provider`, `ServerInfo.media_server.kind` | 1 |
| `media_server_url` | `TINDEERR_MEDIA_SERVER_URL` | `MediaServerConfigInput.url`, `Connector.url` | 1 |
| `media_server_api_key` | `TINDEERR_MEDIA_SERVER_API_KEY` | `MediaServerConfigInput.api_key` (Jellyfin, Emby) or the token from `plex_pin_id` (Plex); `Connector.secret` | 1 (it also holds the Plex owner token) |
| `media_server_verify_tls` | `TINDEERR_MEDIA_SERVER_VERIFY_TLS` | `MediaServerConfigInput.verify_tls`, `Connector.verify_tls` | 2 |
| `public_url` | `TINDEERR_PUBLIC_URL` | `ServerSettings.public_url` | 2 |
| `password_sign_in` | `TINDEERR_PASSWORD_SIGN_IN` | `ServerSettings.password_sign_in` | 2 |
| `language`, `streaming_region`, `daily_generation_limit`, `warm_up_enabled`, `content_filters` | `TINDEERR_<NAME>` | `ServerSettings.*` | stored from 2, used from 4 |

Environment values are parsed and validated with the setting's type at startup (a
boolean setting accepts `true`/`false`, not any non-empty string). The media server's
identity, the install id and the setup code hash are server state, not settings: they
cannot be forced from the environment.

### Authentication

Details in [the authentication reference](auth.md), with the reasons in
[ADR 0004](adr/0004-authentication.md), [ADR 0009](adr/0009-web-console-and-phone-pairing.md),
[ADR 0010](adr/0010-roles-and-refresh-tokens.md) and
[ADR 0011](adr/0011-hardening-after-the-pre-step-2-review.md).

- **Request context.** Client IP (rightmost untrusted hop behind
  `TINDEERR_TRUSTED_PROXIES`), scheme, allowed `Host` and origin are resolved once per
  request by the API layer.
- **Sign-in methods** (both clients): Jellyfin / Emby password, Plex PIN, Jellyfin
  Quick Connect. The app can also be paired from the console by QR code, with an
  approval click in the console.
- **App:** bearer access token (JWT, 15 min) and rotating refresh token. Reusing a
  refresh token revokes the session, with no grace period, so the app refreshes
  single-flight.
- **Console:** `HttpOnly` session cookie, a CSRF token bound to the session on unsafe
  methods, and an `Origin` check. Admin, pairing and account-deletion endpoints accept
  only this.
- **Every request** re-reads its session and user, so revocation is immediate.
- **Roles:** `admin` when the media server says so at the last sign-in, or when a
  Tindeerr admin promoted them. Checked in the database on every admin request. The
  media server connector and `public_url` need a media server administrator with a
  fresh re-authentication.

## Web console

- **Stack:** React, Vite, TypeScript `strict`, in `web/`
  ([ADR 0009](adr/0009-web-console-and-phone-pairing.md)). TanStack Query for server
  state, React Router for routes, i18next with react-i18next for translations. ESLint
  (with `react/no-danger`), Vitest, Playwright for the end-to-end tests. Plain CSS files
  (or CSS modules), no CSS-in-JS that injects `<style>` elements.
- **No npm workspaces.** `web/` has its own `package.json` and `package-lock.json`.
  `shared/i18n/` holds only JSON catalogs, imported through a Vite alias (`@i18n`,
  with `server.fs.allow` for the parent folder).
- **Translations:** i18next JSON v4 catalogs, one file per language and namespace:
  `shared/i18n/<lang>/<namespace>.json` (`common`, `console`; the app adds its own
  namespaces later), nested keys, plurals with the `_one` / `_other` suffixes. English
  and French from the first page.
- **API client:** generated from `api/openapi.yaml` with `openapi-typescript` into
  `web/src/api/schema.d.ts`, **committed**, and used through `openapi-fetch`. CI
  regenerates it and fails if it differs from the committed file.
- **Skeleton (step 2):**
  - a fetch wrapper (an `openapi-fetch` middleware) that sends
    `credentials: "same-origin"`, adds `X-CSRF-Token` from memory on `POST`, `PUT`,
    `PATCH` and `DELETE`, turns problem details into typed errors, sends the user to
    sign-in on `401`, and reloads the session once on `403 csrf_failed`;
  - an auth guard that calls `GET /api/v1/auth/web/session` on load: a `web` session
    opens the console, a `setup` session the wizard; on `401` it reads
    `GET /api/v1/server/info` and shows the claim page when `setup_required` is true,
    the sign-in page otherwise. The CSRF token lives in memory only;
  - routes: `/setup` (claim, media server, admin sign-in, `public_url`), `/sign-in`,
    `/settings`, `/users`, `/connect-phone`, `/sessions`; non-admins only reach the last
    two. Pages for connectors, AI provider and usage come in steps 3 and 4;
  - the QR code is rendered as React SVG elements or on a canvas (for example
    `qrcode.react`'s `QRCodeSVG`), never through `innerHTML`, with the host of
    `public_url` written next to it.
- **Serving:** the server serves `TINDEERR_WEB_DIR` under `/`:
  - `/api/…` never falls back to the console: unknown API paths answer a `404` problem;
  - `/assets/…` serves hashed files; a missing asset is a plain `404`, never
    `index.html`;
  - any other `GET` or `HEAD` that is not a file (and not `/healthz`) returns
    `index.html`, for client-side routing.
- **Security headers depend on the path:**

  | Path | Headers |
  |---|---|
  | `/api/…`, `/healthz`, errors | `Cache-Control: no-store`, `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy: same-origin` |
  | `index.html` (and the fallback) | `Cache-Control: no-store`, the console CSP of ADR 0009, `Cross-Origin-Opener-Policy: same-origin`, `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()`, plus `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy: same-origin` |
  | `/assets/…` | `Cache-Control: public, max-age=31536000, immutable`, `nosniff`, `Cross-Origin-Resource-Policy: same-origin` |
  | other static files (`favicon.ico`, `robots.txt`) | `Cache-Control: no-cache`, `nosniff` |

  Over HTTPS (effective scheme), responses also carry
  `Strict-Transport-Security: max-age=31536000` (no `includeSubDomains`, no
  `preload`). A route that sets its own `Cache-Control` keeps it.
- **Development:** the Vite dev server proxies `/api` to a local server with
  `changeOrigin: false`, so the `Host` and `Origin` stay `localhost` and the console
  stays same-origin; CORS stays disabled.

### Build and CI

- **Image.** `server/Dockerfile` is built from the **repository root**
  (`docker build -f server/Dockerfile .`), because the console needs `web/`,
  `shared/i18n/` and `api/openapi.yaml`. A root `.dockerignore` only lets in what the
  build needs. A Node build stage (image pinned by digest) runs `npm ci` and
  `npm run build` in `web/`; the runtime stage copies `web/dist` to `/app/web` and has no
  Node. The compose example builds with `context: ..` and `dockerfile: server/Dockerfile`.
- **Workflows.**
  - `server.yml`: also triggered by `web/**` and `shared/**`, since the image contains
    the console.
  - `web.yml` (step 2): `npm ci`, ESLint, `tsc --noEmit`, Vitest, `npm run build`, and
    the generated-client check; triggered by `web/**`, `shared/**`, `api/openapi.yaml`.
  - `e2e.yml` (step 2): the end-to-end suite against the built image, described in the
    [roadmap](roadmap.md#step-2-setup-authentication-and-web-console-shell). It runs in
    CI only.

## App

- **Stack:** Expo (React Native), TypeScript `strict`
  ([ADR 0003](adr/0003-mobile-stack.md)).
- **Server state:** TanStack Query. Cached decks, likes and profile make the app open
  instantly.
- **Local state:** Zustand (small UI stores). Votes wait in a persistent queue when
  offline.
- **API client:** generated from `api/openapi.yaml` (`openapi-typescript` +
  `openapi-fetch`). It is never written by hand.
- **Secrets:** tokens are kept in `expo-secure-store` (Android Keystore).
- **Token refresh:** single-flight. One refresh at a time; requests that hit an
  expired token wait for it, then retry. Covered by tests
  ([ADR 0010](adr/0010-roles-and-refresh-tokens.md)).
- **Connect:** type the server URL and sign in, or scan a pairing QR code from the
  console, confirm the host (shown prominently, punycode for internationalised names),
  and wait for the approval click in the console. A server that still needs setup gets
  a message pointing to its console.
- **i18n:** English and French from the first screen.
- **Structure:** by feature (`features/deck`, `features/likes`, `features/taste`,
  `features/settings`, `features/auth`, `features/pairing`), plus `shared/` for UI
  components and API access. No admin screens: admin work is in the web console.
  No Android-only native module without an iOS equivalent, to keep the iOS port cheap.

## API

- **Versioning:** under `/api/v1` ([ADR 0006](adr/0006-api-contract-and-versioning.md)).
- **Errors:** RFC 9457 problem details, with a stable `code` the app switches on.
- **Compatibility:** `GET /api/v1/server/info` returns the server version, the API
  version, the minimum app version, the sign-in methods (`password`, `plex_pin`,
  `quick_connect`, `pairing`) and a list of capabilities. The app hides what the
  server cannot do and asks the user to update when needed.
- **Two credentials:** bearer token (app) or session cookie with CSRF token
  (console). Each operation's `security` in the contract says which it accepts.
  Operations tagged `console` never accept a bearer token (they answer `401`); those
  among them with `security: []` are the public sign-ins that set the cookie.
- **Errors from lower layers.** Auth and storage raise `ProblemError` (from `core`) with
  the status and `code`; one handler renders it. Everything else unhandled is a generic
  `500 internal_error`.
- **Contract checking:** the contract is written first. Contract tests validate the
  responses of implemented endpoints against the committed schemas, and fail when the
  server exposes a route or method that `api/openapi.yaml` does not document. The
  generated spec is not compared byte for byte: that proved brittle.

## Repository layout

```
tindeerr/
├── api/openapi.yaml        contract, source of truth for both sides
├── server/                 Python package, Dockerfile (built from the repo root), tests
├── web/                    web console (React, Vite), built into the server image
├── app/                    Expo app, tests, Maestro flows
├── shared/i18n/            translation catalogs used by web/ and app/
├── docs/                   architecture, security, roadmap, ADRs
├── .dockerignore           what the image build may read
└── .github/workflows/      CI per side (server, web, e2e) + contract check + releases
```
