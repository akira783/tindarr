# Architecture

## Overview

```
 Android app (Expo)                     Tindarr server (one per household)
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
server issued. There is no central Tindarr service.

The server has two clients ([ADR 0009](adr/0009-web-console-and-phone-pairing.md)):

- **The web console,** served by the server itself, for setup and all admin work,
  and for pairing phones. It is used from a computer, rarely.
- **The mobile app,** for daily use: the deck, likes, taste profile, preferences
  and sessions.

## Server

### Layers

| Layer | Package | Depends on | Rule |
|---|---|---|---|
| Main | `tindarr.main` | everything | Composition root: builds the adapters, hands them to the domain as ports, starts the jobs. |
| API | `tindarr.api` | domain, auth, storage, ports, core | HTTP only: request context (client IP, scheme, allowed hosts), cookies, CSRF and `Origin` checks, parsing, status codes, problem details, security headers, serving the console's static files. No business logic. |
| Jobs | `tindarr.jobs` | domain, auth, storage | Background work with persisted state. |
| Auth | `tindarr.auth` | ports (media server, plex.tv), storage, core | Pure functions and services: sessions (mobile, web, setup), tokens, handles, pairing, roles, rate-limit decisions, user sync. No Starlette or FastAPI import (import-linter enforces it): it never sees a request, a cookie or a header, only values the API layer extracted. |
| Domain | `tindarr.swipe` | ports (interfaces), storage | Swipe engine, prompts, scoring. No HTTP, no vendor SDK. |
| Adapters | `tindarr.adapters.*` | ports, vendor SDKs, httpx | One package per external system. |
| Ports | `tindarr.ports` | nothing | `Protocol` classes the domain and auth talk to. |
| Storage | `tindarr.storage` | SQLAlchemy Core | Tables, repositories, Alembic migrations. |
| Core | `tindarr.core` | nothing | Bootstrap configuration, key derivation, encryption, logging, and `ProblemError(status, code, detail)`, which auth and storage raise and one API handler renders. Usable by every layer. |

Imports only point downwards. Only `tindarr.main` imports adapters. CI enforces this
with `import-linter`.

### Ports

```python
class MediaServer(Protocol):
    kind: Literal["jellyfin", "emby", "plex"]
    # Step 2: identity, sign-in and users.
    async def identify(self) -> ServerIdentity                       # server id, product, version
    async def test(self) -> ConnectorStatus                          # coarse result, never a body
    async def authenticate_password(self, username: str, password: str) -> MediaUser  # Jellyfin / Emby
    async def quick_connect_enabled(self) -> bool                    # feeds auth_methods, cached
    async def quick_connect_start(self) -> QuickConnectStart         # Jellyfin: code + secret
    async def quick_connect_poll(self, secret: str) -> MediaUser | None  # None while not approved
    async def list_users(self) -> list[MediaUser]                    # hourly sync
    # Step 3: what the swipe engine reads.
    async def library_ids(self) -> LibraryIndex                      # TMDb ids already owned
    async def engagement(self, user: MediaUser) -> list[Engagement]  # watched / mostly watched /
                                                                     # in progress / paused / abandoned
    def deep_link(self, item: LibraryItem) -> str | None              # "open in Jellyfin"

class PlexTv(Protocol):            # step 2; used by auth and by the Plex adapter
    # ``device_name`` is what plex.tv shows on the approval page ("Tindarr (Chez nous)").
    async def create_pin(self, client_id: str, device_name: str) -> PlexPin
    async def check_pin(self, pin: PlexPin) -> str | None            # the token once approved
    async def account(self, token: str) -> PlexAccount               # plex.tv account id, name
    async def resources(self, token: str) -> list[PlexResource]      # clientIdentifier, owned
    async def delete_device(self, token: str, client_id: str) -> bool  # unofficial, best effort
    async def shared_users(self, owner_token: str, machine_id: str) -> list[PlexAccount]

class PublicUrlProbe(Protocol):    # step 2; the one call Tindarr makes to its own address
    async def fetch_proof(self, public_url: str, nonce: str) -> ProofResponse  # no redirect, 5 s


class RequestBackend(Protocol):    # Seerr v3; Jellyseerr legacy, Overseerr for Plex
    async def test(self) -> ConnectionCheck
    async def find_user(self, media_user: MediaUser) -> BackendUser | None
    async def request(self, title: TitleRef, on_behalf_of: BackendUser) -> RequestResult
    async def status(self, titles: Sequence[TitleRef]) -> dict[TitleRef, Availability]

class Metadata(Protocol):          # TMDb
    async def test(self) -> ConnectionCheck
    async def search(self, query: SearchQuery) -> list[Title]
    async def match(self, query: SearchQuery) -> Title | None        # search, then pick one
    # Step 4.2: the two calls the candidate pool is built from.
    async def discover(self, query: DiscoverQuery) -> list[Title]    # filtered, one page
    async def related(self, ref: TitleRef, language: str, page: int = 1) -> list[Title]
    async def excluded_genre_ids(self, filters: TitleFilters) -> frozenset[int]
    async def details(self, ref: TitleRef, language: str) -> TitleDetails
    async def watch_providers(self, ref: TitleRef, region: str) -> list[Provider]
    async def trailer(self, ref: TitleRef, language: str) -> Trailer | None
    async def region_providers(self, region: str) -> list[Provider]

class RatingsSource(Protocol):     # OMDb, the optional enricher of the metadata port
    async def test(self) -> ConnectionCheck
    async def ratings(self, imdb_id: str) -> Ratings | None

class LlmProvider(Protocol):
    kind: LlmProviderKind
    capabilities: LlmCapabilities   # json_schema | json_mode | text
    async def test(self) -> ConnectionCheck
    async def list_models(self) -> list[str]
    async def generate[T: BaseModel](self, prompt: Prompt, schema: type[T]) -> Generation[T]
```

Every connector answers the same `ConnectionCheck` (`tindarr.ports.connectors`): a coarse
health value and, at most, the remote product's name and version — never a response body
([the security model](security.md#7-admin-configured-urls-ssrf)).

`MediaUser` carries the normalised user id, the name, and the `is_admin`,
`remote_access` and `disabled` flags. Adapters own the media server's wire details
(the `Authorization: MediaBrowser …` header, `X-Plex-Token`); see
[the authentication reference](auth.md#4-sign-in-through-the-media-server).

Adding a media server, request backend or AI provider means adding one adapter
package and its contract tests. The domain does not change
([ADR 0005](adr/0005-adapters-and-ai-providers.md)).

### Swipe engine

The fork's logic, with its middle turned around by
[ADR 0013](adr/0013-recommendation-engine.md): TMDb retrieves, the model chooses.

1. **Signals.** Engagement from the media server (watched, abandoned, in progress,
   "mostly watched" for a series at 60 % of its episodes or more), the library, past votes and the taste profile.
   Media server play counts are never used as a rewatch signal: debrid setups inflate
   them. From step 4.4, **what the household watched outside the media server** joins
   the same two signals (below): imported history and calibration ticks are excluded
   from the pool like a vote, and the imported half carries engagement like the media
   server's.
2. **Retrieval** (`tindarr.swipe.retrieval`). TMDb builds the candidate pool: what it
   recommends to somebody who liked this user's recent likes, plus filtered discovery
   under the **novelty band** — an adaptive popularity floor that drops as the setting
   goes from `familiar` to `bold`, with a fame ceiling in vote counts at the bold end.
   **Every exclusion is applied here**, before the model sees anything: voted on,
   already served, owned, wrong media type, outside the household's content filters.
3. **Batch prompt** (`tindarr.swipe.hybrid`). Mode (`calibration` until 15 votes, then
   `normal`), novelty, optional mood, the taste profile, what they actually watched and
   the recent votes — the fork's prompt, minus its "never propose" list, which the pool
   has already made unnecessary. The model answers with **TMDb ids from the pool**, a
   rationale and a pick kind, validated against a schema; an id that was not offered is
   dropped, and there is nothing left to search for. A model that fails costs the
   sentences, not the batch: the pool is served in its own order.
4. **Enrichment.** Translation into the user's language, streaming providers for the
   region, ratings (OMDb), trailer — read once for the batch and stored on the card.
   What depends on the moment the card is *shown* is computed then instead: the "on your
   services" flag, from the preferences as they stand, and the availability badge, from
   the request backend. So ticking a service or filing a request updates cards somebody
   is already holding.
5. **Profile.** Rewritten in the background every ten opinions, as short "Loves / Avoids
   / Nuances" bullets. Unlike the batch prompt, this one names titles: a vote row carries
   the title the server copied off its own card. Text written by the user is kept in its
   own column and never rewritten.

#### What the household has already watched, from outside the deck

ADR 0013 measured the already-seen problem — 47 % of the fork's cards — and measured
every cure. Importing what somebody watched elsewhere was worth **3 of those 47**, so
imports are kept as a *taste* source and the already-seen problem is answered by the
novelty band and by a **calibration grid**. Both write the same rows
(`tindarr.ports.history`, table `watch_history`), one per source per title.

- **File imports** (`tindarr.swipe.imports`). A Netflix viewing history (the short
  export or the long GDPR one), an IMDb ratings export, a Letterboxd archive or one of
  its CSVs. The format is detected from the header, never asked for. The upload is the
  request body, bounded as it arrives, parsed in that request and **never stored** —
  neither the bytes nor the file name. Identifying the titles runs in a background task
  (`tindarr.jobs.imports`), one per user and two per server, and an interrupted one is
  closed at the next startup.
  - Netflix rows are prose: the title is split on `": "` and **never** on a French
    `" : "` in any whitespace form, a season marker settles the media type, two
    different trailing parts under one title settle it by counting, and trailers are
    dropped (the long export's own column, the short one's vocabulary).
  - A row that does not clearly name one title goes to a **review queue** with what
    TMDb offered, best first. Nothing is guessed: `results[0]` is the fallback nowhere.
    An IMDb `tt…` id is resolved exactly through `/find` and never searched for.
  - Episodes are counted per series against TMDb's total and turned into the media
    server port's own `EngagementState`, with its own thresholds. A series a file
    mentions without naming an episode is recorded as seen and carries **no** state:
    "they watched something of it" is not "they finished it".
- **Forgetting one.** Every history row names the upload that wrote it, so deleting an
  import deletes what *it* said and leaves a second import of the same format, the other
  sources and the grid's answers standing.
- **The calibration grid** (`tindarr.swipe.calibration`). A wall of famous posters to
  tick, ranked by TMDb vote count rather than by this week's popularity, spread across
  five decades and across what is famous in the household's own language, and capped so
  no genre takes more than a third of it. Everything already answered — a poster the
  user said no to included — is gone before the wall is built.

Neither is a **vote**. They are read as `StrategyContext.known` (excluded from the pool)
and as `engagement` (taste); the statistics, the vote count and the deck's calibration
progress never see them.

**When a vote happened.** A queued vote carries the moment it was cast, which is the
honest timestamp and the one the skip cool-down runs from — but it is a value the client
chooses, so it is clamped at both ends: never after now (a drifting clock would park a
vote at the top of every ordering), and never before the card was served (a vote cannot
predate the card it is about, and a backdated `skip` would otherwise be a way to ask for
its own cool-down to be over). The stored answer is then the newest **swipe**, not the
newest packet: a queue a phone held for three weeks cannot overwrite an opinion changed
since from a browser.

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

- **Batch generation** (from step 4.5). Started when the deck runs low or has nothing to
  show. One per user at a time — the job row *is* the lock, taken inside SQLite's write
  transaction, so two polls a millisecond apart buy one batch — and two at a time for the
  whole server. The deck answers `202` while one runs.
- **Warm-up** (from step 4.5). 30 s after startup, then every 6 h, only for users active
  in the last 14 days, only when they have neither a waiting batch nor enough cards left,
  and only while the server has a free slot: a household that has all come home at once
  is better served by their own polls than by a sweep holding every slot.
- **Profile rewrite** (from step 4.5). Debounced on the vote count (ten new opinions),
  one per user at a time, and charged against the same daily cap as a batch — it is the
  same one model call.
- **Per-user limits.** A maximum number of generations per day, set by the admin, on
  top of the one-at-a-time rule above (see [security](security.md)). It is charged
  **before** the provider is called, in the transaction that claims the job, and it is
  **not** refunded when the call fails: "it failed" is exactly the state a retry loop is
  in, and a refund there turns one bad minute at a provider into an unbounded number of
  paid attempts.
- **Swipe purge** (from step 4.5). Daily: jobs that have claimed to be running for half
  an hour (the companion of the startup sweep, for a task that hung rather than died),
  served cards nobody voted on a month later, batches nobody ever spent, finished job
  rows, and the vote receipts of queues no phone can still be holding.

**Two cool-offs decide when the deck pays again.** After a failed generation it waits
five minutes, because a deck is polled every two and a half seconds and a provider
refusing a key would otherwise spend a day's cap in thirty of them. After a batch that
came back **empty** it waits an hour, or until a vote changes the pool — and the deck
says `exhausted` so the client can tell "no cards yet" from "no cards at all".
- **Media server user sync** (from step 2). 60 s after startup, then hourly: disables
  users removed or disabled on the media server, clears lost admin flags, checks the
  server's identity ([auth reference](auth.md#periodic-sync)).
- **Handle sweep** (from step 2). Every 30 s, in memory: drops expired sign-in handles
  and logs out Quick Connect approvals nobody collected.
- **Purge** (from step 2). Daily: old revoked sessions, refresh tokens and pairings.
- **File imports** (from step 4.4). Not scheduled: an upload hands one over and the task
  runs until it is done. Two at a time for the whole server, one per user, and every row
  left `running` by a restart is closed as failed at startup.

### Storage

SQLite in WAL mode by default, one file in the data volume. It is accessed through
SQLAlchemy Core (no ORM), so PostgreSQL remains an option without a rewrite. Alembic
migrations run at startup, and a backup copy is taken first when the schema version
changes.

Main tables: `server_state` (single row: install id, setup state, media server
identity), `settings` (secrets encrypted), `users` (with `media_server_admin` and
`promoted`), `sessions` (`kind`: `mobile`, `web` or `setup`), `refresh_tokens`,
`pairings`, `watch_history` (what a user watched outside the deck: an import, a grid
tick), `imports` and `import_reviews`, and from step 4.5 `batches`, `cards`, `votes`,
`vote_receipts`, `taste_profiles`, `preferences` (including the user's streaming
services), `jobs`, `llm_usage` and `region_providers` (the cache of the region's
streaming services). The step-2 columns are specified in
[the authentication reference](auth.md#12-tables).

Three of those are worth a line each, because they are where the swipe engine's rules
actually live:

- **`cards`** carries the enrichment that does not move (the translation, the providers
  TMDb listed, the ratings, the trailer) and nothing that depends on who is looking. A
  card is stamped `served_at` the first time it leaves the server: that is what feeds the
  next prompt's "already shown" list, what starts its 24 h in the deck, and what the
  purge measures a month from. A **voted-on** card is never purged — the vote points at
  it, and the pick type on that row is what a statistic is counted from.
- **`vote_receipts`** is separate from `votes` because the two outlive each other. A vote
  is replaced when somebody changes their mind and deleted when they undo; "have I
  already stored this queued item?" must stay answerable either way, or a phone
  reconnecting would resurrect a vote the user has since undone.
- **`taste_profiles`** has two text columns. `user_text` is what the person typed and
  nothing but a user edit writes it; `text` is what the last rewrite produced; the
  profile the engine reads is the two of them, the person's first. That is what makes
  "a rewrite never contradicts what the user wrote" a property of the storage rather
  than an instruction in a prompt.

No `translations` table: a card is generated in the user's language and stores what TMDb
answered, so there is nothing left to translate at serve time. It would be worth one the
day a household reads the same batch in two languages, which nothing asks for yet.

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
| `TINDARR_DATA_DIR` | `data` (`/data` in the image) | Database, backups, key, setup code. |
| `TINDARR_SECRET_KEY` | generated into `<data>/secret.key` | Master key material. |
| `TINDARR_HOST`, `TINDARR_PORT` | `127.0.0.1` (`0.0.0.0` in the image), `8787` | Listening address. |
| `TINDARR_LOG_LEVEL` | `INFO` | |
| `TINDARR_API_DOCS` | `false` | Interactive docs at `/api/docs`. |
| `TINDARR_DB_BACKUPS_KEEP` | `5` | Pre-migration backups kept. |
| `TINDARR_TRUSTED_PROXIES` | none | IPs/CIDRs whose `X-Forwarded-For`/`-Proto` are honoured ([rules](auth.md#client-ip-and-scheme)). |
| `TINDARR_ALLOWED_HOSTS` | none | Extra host names accepted in `Host`, besides IP literals, `localhost` and the host of `public_url` ([rules](auth.md#allowed-hosts-dns-rebinding)). Step 2. |
| `TINDARR_ALLOW_HTTP_CONSOLE` | `false` | Console over plain HTTP from private client IPs ([rules](auth.md#cookies)). Step 2. |
| `TINDARR_WEB_DIR` | `/app/web` | Built console. When it holds no `index.html` (development without a build), nothing is intercepted and every path outside the API answers the router's own `404`. Step 2. |

Settings, with their environment variable and where they appear in the contract (the
names differ for historical reasons; this table is the mapping):

| Setting | Environment variable | Contract | Step |
|---|---|---|---|
| `server_name` | `TINDARR_SERVER_NAME` | `ServerSettings.name`, `ServerInfo.name` | 1 |
| `media_server_kind` | `TINDARR_MEDIA_SERVER_KIND` | `MediaServerConfigInput.server_type`, `Connector.provider`, `ServerInfo.media_server.kind` | 1 |
| `media_server_url` | `TINDARR_MEDIA_SERVER_URL` | `MediaServerConfigInput.url`, `Connector.url` | 1 |
| `media_server_api_key` | `TINDARR_MEDIA_SERVER_API_KEY` | `MediaServerConfigInput.api_key` (Jellyfin, Emby) or the token from `plex_pin_id` (Plex); `Connector.secret` | 1 (it also holds the Plex owner token) |
| `media_server_verify_tls` | `TINDARR_MEDIA_SERVER_VERIFY_TLS` | `MediaServerConfigInput.verify_tls`, `Connector.verify_tls` | 2 |
| `public_url` | `TINDARR_PUBLIC_URL` | `ServerSettings.public_url` | 2 |
| `password_sign_in` | `TINDARR_PASSWORD_SIGN_IN` | `ServerSettings.password_sign_in` | 2 |
| `language`, `streaming_region`, `daily_generation_limit`, `warm_up_enabled`, `content_filters` | `TINDARR_<NAME>` | `ServerSettings.*` | stored from 2, used from 4 |

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
  `TINDARR_TRUSTED_PROXIES`), scheme, allowed `Host` and origin are resolved once per
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
  Tindarr admin promoted them. Checked in the database on every admin request. The
  media server connector and `public_url` need a media server administrator with a
  fresh re-authentication.

## Web console

- **Stack:** React, Vite, TypeScript `strict`, in `web/`
  ([ADR 0009](adr/0009-web-console-and-phone-pairing.md)). TanStack Query for server
  state, React Router for routes, i18next with react-i18next for translations. ESLint
  (type-aware, with `@eslint-react` — including its rule against
  `dangerouslySetInnerHTML` — `eslint-plugin-react-hooks`, a11y rules, and a ban on
  `innerHTML`, `eval` and browser storage outside the preferences module), Vitest with
  Testing Library, Playwright for the end-to-end tests. Plain CSS files (or CSS
  modules), no CSS-in-JS that injects `<style>` elements. The QR code comes from
  `qrcode.react` (`QRCodeSVG`, React elements only).
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
- **Serving:** the server serves `TINDARR_WEB_DIR` under `/`:
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
  | other static files (`favicon.svg`, `robots.txt`) | `Cache-Control: no-cache`, `nosniff` |

  With `TINDARR_HSTS` on, responses also carry
  `Strict-Transport-Security: max-age=31536000` (no `includeSubDomains`, no `preload`);
  it is an opt-in rather than a scheme test, because a server behind a TLS-terminating
  proxy only sees plain HTTP. A route that sets its own `Cache-Control` or
  `Content-Security-Policy` keeps it.
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
  - `web.yml` (step 2): `npm ci --ignore-scripts`, ESLint, `tsc --noEmit`, Vitest with
    coverage, `npm run build` (Vite plus a check that the built page carries no inline
    script or style, no `data:` URI and no third-party origin), the generated-client
    check and `npm audit`; triggered by `web/**`, `shared/**`, `api/openapi.yaml`.
  - `e2e.yml` (step 2): Playwright (Chromium) drives the console of the **built image**
    against real Jellyfin and Emby containers pinned by digest, described in the
    [roadmap](roadmap.md#step-2-setup-authentication-and-web-console-shell). Three
    targets run: Jellyfin 12, the oldest Jellyfin supported (10.10) and Emby. Plex is a
    separate job, `workflow_dispatch` only, because it needs a real plex.tv account —
    and a pull request from a fork never sees repository secrets. The same workflow
    lints the workflows (`actionlint`) and the CI shell scripts (`shellcheck`).
  - **Running the end-to-end stack yourself.** `.github/scripts/e2e-stack.sh up` starts
    the media servers, seeds them through their own first-run wizard APIs
    (`seed-media-server.py`) and starts one Tindarr server per target, all on loopback
    in the host network namespace: the browser, the server's own `public_url` check and
    the media server URL then all use the same addresses. `E2E_IMAGE=source` runs the
    server from the working tree with uv instead of an image, which needs no image
    build; `npm run build` in `web/` first. Then, in `web/`:
    `set -a; . ../e2e.env; set +a; npm run test:e2e`.

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
  among them that need no session (`security: []`, an empty `{}` alternative, or only
  the pre-auth cookie) are the setup claim and the web sign-ins that set the cookie.
- **Errors from lower layers.** Auth and storage raise `ProblemError` (from `core`) with
  the status and `code`; one handler renders it. Everything else unhandled is a generic
  `500 internal_error`.
- **Contract checking:** the contract is written first. Contract tests validate the
  responses of implemented endpoints against the committed schemas, and fail when the
  server exposes a route or method that `api/openapi.yaml` does not document. The
  generated spec is not compared byte for byte: that proved brittle.

## Repository layout

```
tindarr/
├── api/openapi.yaml        contract, source of truth for both sides
├── server/                 Python package, Dockerfile (built from the repo root), tests
├── web/                    web console (React, Vite), built into the server image
├── app/                    Expo app, tests, Maestro flows
├── shared/i18n/            translation catalogs used by web/ and app/
├── docs/                   architecture, security, roadmap, ADRs
├── .dockerignore           what the image build may read
└── .github/workflows/      CI per side (server, web, e2e) + contract check + releases
```
