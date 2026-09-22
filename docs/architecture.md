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
| API | `tindeerr.api` | domain, auth | HTTP only: parsing, status codes, problem details, serving the console's static files. No business logic. |
| Jobs | `tindeerr.jobs` | domain, storage | Background work with persisted state. |
| Auth | `tindeerr.auth` | ports (media server), storage | Sessions (mobile, web, setup), tokens, cookies and CSRF, pairing, roles. |
| Domain | `tindeerr.swipe` | ports (interfaces), storage | Swipe engine, prompts, scoring. No HTTP, no vendor SDK. |
| Adapters | `tindeerr.adapters.*` | ports, vendor SDKs, httpx | One package per external system. |
| Ports | `tindeerr.ports` | nothing | `Protocol` classes the domain and auth talk to. |
| Storage | `tindeerr.storage` | SQLAlchemy Core | Tables, repositories, Alembic migrations. |
| Core | `tindeerr.core` | nothing | Bootstrap configuration, key derivation, encryption, logging. Usable by every layer. |

Imports only point downwards. Only `tindeerr.main` imports adapters. CI enforces this
with `import-linter`.

### Ports

```python
class MediaServer(Protocol):
    kind: Literal["jellyfin", "emby", "plex"]
    async def authenticate(self, credentials) -> MediaUser          # password, Plex PIN or Quick Connect
    async def list_users(self) -> list[MediaUser]
    async def library_ids(self) -> LibraryIndex                      # TMDb ids already owned
    async def engagement(self, user: MediaUser) -> list[Engagement]  # watched / abandoned / in progress
    def deep_link(self, item: LibraryItem) -> str | None              # "open in Jellyfin"

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

### Storage

SQLite in WAL mode by default, one file in the data volume. It is accessed through
SQLAlchemy Core (no ORM), so PostgreSQL remains an option without a rewrite. Alembic
migrations run at startup, and a backup copy is taken first when the schema version
changes.

Main tables: `users` (with `media_server_admin` and `promoted`), `sessions` (`kind`:
`mobile`, `web` or `setup`), `pairings`, `settings` (secrets encrypted), `batches`,
`cards`, `votes`, `taste_profiles`, `preferences` (including the user's streaming
services), `jobs`, `llm_usage`, `translations`, plus a cache of the region's
streaming providers.

### Configuration

Settings are edited by the admin from the web console and stored in the database,
secrets encrypted. Any setting can also come from an environment variable
(`TINDEERR_<NAME>`, or `_FILE` for Docker secrets). A value set that way is shown as
locked in the console. `public_url` (the origin phones reach the server at) is one
of them; it is only used to build pairing QR codes. Only the data
directory and the encryption key have to come from outside the database.

### Authentication

Details in [ADR 0004](adr/0004-authentication.md), [ADR 0009](adr/0009-web-console-and-phone-pairing.md)
and [ADR 0010](adr/0010-roles-and-refresh-tokens.md).

- **Sign-in methods** (both clients): Jellyfin / Emby password, Plex PIN, Jellyfin
  Quick Connect. The app can also be paired from the console by QR code.
- **App:** bearer access token (JWT, 15 min) and rotating refresh token. Reusing a
  refresh token revokes the session, with no grace period, so the app refreshes
  single-flight.
- **Console:** `HttpOnly` session cookie, a CSRF token bound to the session on unsafe
  methods, and an `Origin` check. Admin and pairing endpoints accept only this.
- **Roles:** `admin` when the media server says so at the last sign-in, or when a
  Tindeerr admin promoted the user. Checked in the database on every admin request.

## Web console

- **Stack:** React, Vite, TypeScript `strict`, in `web/`
  ([ADR 0009](adr/0009-web-console-and-phone-pairing.md)). TanStack Query for server
  state. The API client is generated from `api/openapi.yaml` like the app's, and the
  translations come from `shared/i18n/`.
- **Serving:** built in the server image's build stage, served by the server as
  static files under `/`; unknown `GET` paths outside `/api` and `/healthz` return
  `index.html`. Strict Content-Security-Policy, no inline script.
- **Pages:** first-run setup (claim, media server, then the other connectors and
  `public_url`), sign-in, connectors, AI provider and model, server settings, users,
  usage, "Connect a phone" and the user's own sessions. Non-admins only see the last
  two.
- **Development:** the Vite dev server proxies `/api` to a local server, so the
  console stays same-origin and CORS stays disabled.

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
  console. A server that still needs setup gets a message pointing to its console.
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
  (console). Each operation's `security` in the contract says which it accepts;
  operations tagged `console` refuse bearer tokens.
- **Contract checking:** the contract is written first. Contract tests validate the
  responses of implemented endpoints against the committed schemas, and fail when the
  server exposes a route or method that `api/openapi.yaml` does not document. The
  generated spec is not compared byte for byte: that proved brittle.

## Repository layout

```
tindeerr/
├── api/openapi.yaml        contract, source of truth for both sides
├── server/                 Python package, Dockerfile, tests
├── web/                    web console (React, Vite), built into the server image
├── app/                    Expo app, tests, Maestro flows
├── shared/i18n/            translation catalogs used by web/ and app/
├── docs/                   architecture, security, roadmap, ADRs
└── .github/workflows/      CI per side + contract check + releases
```
