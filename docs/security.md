# Security model

Targets: OWASP ASVS 4 level 2 for the server and the web console, OWASP MASVS L1 for
the app.

## Assets

| Asset | Where | Impact if leaked or abused |
|---|---|---|
| AI provider API keys | server DB (encrypted) | Direct cost for the owner |
| Media server admin API key / Plex owner token | server DB (encrypted) | Full control of the media server |
| Request backend API key | server DB (encrypted) | Unlimited requests, disk fill, quota bypass |
| TMDb / OMDb keys | server DB (encrypted) | Key revoked by the provider |
| Watch history, votes, taste profile | server DB | Privacy |
| Access / refresh tokens | app secure storage, hashed on server | Account takeover |
| Web session cookie | browser (`HttpOnly`), hashed on server | Account takeover; admin rights for an admin |
| Setup code, setup session | server logs and `data/setup-code`; cookie | Claim of an unconfigured server |
| Pairing codes | QR code on screen, hashed on server, 5 min | Account takeover |
| Media server passwords | never stored | Account takeover on the media server |

## Adversaries and mitigations

### 1. Internet attacker against an exposed server

- **Unclaimed fresh install.** The server starts in setup mode and prints a one-time
  **setup code** to its logs, and writes it to `data/setup-code` with mode 0600. The
  code is random, at least 60 bits (12 base32 characters), and claim attempts are
  rate-limited per IP and globally, so it cannot be brute-forced. The code is entered
  in the web console, which exchanges it for a setup session (cookie, 30 minutes).
  Every setup endpoint requires that session, so the first stranger to find the URL
  cannot claim the instance. The code expires as soon as setup completes
  ([ADR 0004](adr/0004-authentication.md), [ADR 0009](adr/0009-web-console-and-phone-pairing.md)).
  The app never handles the setup code.
- **Credential stuffing.** The login rate limit applies per IP and per username, with
  exponential backoff. Errors are identical for an unknown user and a wrong password.
  The media server enforces its own lockout too.
- **Token theft in transit.** HTTPS is expected. Plain HTTP is accepted by the app only
  for private and link-local addresses and `.local` names, after an explicit warning.
  The console refuses to sign in over plain HTTP (`https_required`), except on
  `localhost` or when the operator sets `TINDEERR_ALLOW_HTTP_CONSOLE=true`, which only
  applies to requests from private and link-local addresses and is logged at startup.
- **Forged or replayed tokens.** Access tokens are JWTs, short-lived (15 min), signed
  with a server key and carrying a session id. Refresh tokens are opaque (256 bits) and
  stored as SHA-256 hashes. They rotate on every use: presenting a refresh token that
  was already used revokes the whole session (reuse detection). There is no grace
  period, even for a reuse a second after rotation. The app must therefore refresh
  single-flight (one refresh in flight, other requests wait for it); this is tested
  in the app ([ADR 0010](adr/0010-roles-and-refresh-tokens.md)).
- **Surface.** Only `/healthz`, `/api/v1/server/info`, the sign-in endpoints
  (password, Plex PIN, Quick Connect, for the app and the console), the pairing
  preview and exchange, token refresh and the setup claim are public, plus the
  console's static files. `server/info` returns no user data and no internal URLs.
  Every public endpoint is rate-limited per IP.
- **Plex PIN and Quick Connect hijacking.** The `pin_id` or `handle` a client polls
  with is a random server-side handle (at least 128 bits), not the plex.tv PIN id or
  the Jellyfin Quick Connect secret, and it works once. Guessing it to catch someone
  else's sign-in is not practical.
- **Pairing code brute force.** Pairing codes have at least 128 bits, live 5
  minutes, work once and are stored hashed. `POST /auth/pair` and its preview are
  rate-limited per IP and globally. Unknown, used, expired and revoked codes get the
  same answer (`pairing_expired`).

### 2. Another user of the same server

- **Isolation.** Every row carries `user_id`. Repositories take the user from the
  session, never from the request body. Tests check that one user cannot read or
  change another user's votes, likes, profile or sessions.
- **Request queue abuse.** Requests are filed **as the matching request-backend
  user**: the admin API key is sent with `X-API-User: <backend user id>`, so the backend
  applies that user's request permissions, quotas, auto-approval and override rules.
  The `userId` field of the request body is not used: the backend then still checks
  auto-approval against the API key's admin, so every request would be approved
  without review. No user gets the admin key's rights. A title can only be requested if
  it was served to that user or voted on by them.
- **Cost abuse.** Each user has a daily generation cap, set by the admin, and one
  running generation at a time. Token usage is recorded in `llm_usage` and shown to the
  admin.
- **Admin rights.** A user is admin when the media server says so at their last
  sign-in (re-read at every sign-in) or when a Tindeerr admin promoted them
  ([ADR 0010](adr/0010-roles-and-refresh-tokens.md)). Someone removed as administrator
  on the media server loses Tindeerr admin at their next sign-in, unless promoted
  here. A demotion done only in Tindeerr lasts until that media server
  administrator's next sign-in, and only a media server administrator can demote or
  disable another one, so a promoted admin cannot lock them out. The last enabled
  admin cannot be demoted or disabled. The role is checked in the database on every
  admin request, so a change applies at once. Only admins can see settings (secrets
  are masked: `set` + last 4 characters, never the value), change connectors or run
  connection tests, and only from the web console: a bearer token from a phone is
  refused on admin endpoints.

### 3. Stolen or lost phone

- Tokens live in the Android Keystore through `expo-secure-store`. They are never
  written to logs, crash reports or backups (`allowBackup=false`).
- Each device is a separate session. Users can list and revoke their sessions (from
  the app or the console), and admins can revoke all of a user's sessions.
- A phone's token cannot reach admin or pairing endpoints, so a stolen phone of an
  admin cannot change connectors or pair another device.
- Refresh tokens expire after 60 days of inactivity.

### 4. Attacks through the browser against the web console

- **Session theft by script (XSS).** The session token is only in an `HttpOnly`
  cookie; no token is kept in `localStorage` or readable by JavaScript. The console
  renders server data as text through React, never as HTML. Its
  Content-Security-Policy is strict: `default-src 'self'`, `script-src 'self'` (no
  inline script, no `eval`), `img-src 'self' https://image.tmdb.org`,
  `connect-src 'self'`, `object-src 'none'`, `base-uri 'none'`,
  `form-action 'self'`, `frame-ancestors 'none'`,
  `require-trusted-types-for 'script'`. No third-party script or font.
- **Cookie.** `__Host-tindeerr_session`: `Secure`, `HttpOnly`, `SameSite=Strict`,
  `Path=/`, no `Domain`, so a sibling subdomain cannot set or read it. It holds an
  opaque 256-bit token stored hashed. Web sessions expire after 24 h idle and 7 days
  after sign-in.
- **CSRF.** Three layers: `SameSite=Strict`; a CSRF token bound to the server-side
  session, sent as `X-CSRF-Token` on every `POST`, `PUT`, `PATCH` and `DELETE` and
  compared in constant time; and an `Origin` check (the server's own origin or
  `public_url`, a missing `Origin` is refused). The web sign-in endpoints and the
  setup claim check `Origin` too, against login CSRF. Requests authenticated by a
  bearer header skip the CSRF check (a browser never adds that header by itself),
  and then the cookie is ignored. CORS is disabled.
- **Clickjacking.** `frame-ancestors 'none'` and `X-Frame-Options: DENY`.
- **Caching.** API responses are `no-store`; so is `index.html`. Only hashed static
  assets are cached.

### 5. Phone pairing and Quick Connect

- **Photo or shoulder-surfing of the QR code.** The code lives 5 minutes, works once,
  and the console shows which device used it ("Pixel 9 connected") with a revoke
  button. The session also appears in the user's session list.
- **Phishing a victim into scanning an attacker's QR code.** The victim's app would be
  signed in to the attacker's server, as the attacker's user, and could send it votes
  and moods. Before accepting, the app shows the server URL and "You will be signed in
  as <name>" (from `POST /auth/pair/preview`) and asks for confirmation, and warns when
  it would replace an existing connection. It refuses an `http` server URL unless the
  host is private, link-local or `.local`.
- **Another app catching the link.** Any Android app can register the `tindeerr://`
  scheme, so a code opened from the system camera could be intercepted. The app's own
  scanner is the main path; the short lifetime and the device shown in the console
  limit what an intercepted code gives.
- **No short typed code.** Pairing is QR-only, so there is no low-entropy code on the
  public surface; users who cannot scan sign in normally.
- **Quick Connect phishing.** An attacker could start a Quick Connect sign-in and
  convince a victim to approve the code in their Jellyfin client. This is inherent to
  Quick Connect (Jellyfin's own clients have the same risk). The code expires, the
  handle works once, and the console's session list shows the new session. Admins
  can leave Quick Connect disabled in Jellyfin, which removes the method.

### 6. Untrusted data from external systems (LLM, TMDb, media server)

- **LLM output.** It is validated against a Pydantic schema, and invalid output is
  retried at most once and then reported. It is only ever used as text: titles to look
  up on TMDb, rationales displayed as plain text. It is never rendered as HTML, never
  used as a URL, never executed and never allowed to choose tools.
- **Prompt injection.** Text from TMDb or the user (overview, mood, profile) can steer
  a batch's content, but it cannot reach secrets or other users. The prompt contains
  no key and no other user's data.
- **Images and trailers.** Image URLs are built by the server from TMDb paths and an
  allow-listed base. Trailers are only built from YouTube keys, and the app opens them
  through `youtube-nocookie.com`.

### 7. Admin-configured URLs (SSRF)

The admin supplies the media server, request backend, OpenAI-compatible and Ollama
URLs, and the server calls them. That is intended and restricted to admins.

- **No reflection.** Connection tests return a coarse result (`ok`, `unauthorized`,
  `unreachable`, `unexpected_response`) plus, at most, the remote product name and
  version, never a response body. The model list only returns ids parsed from the
  provider's expected JSON shape. Neither can be used to read internal pages.
- **No secret replay.** When a test, a model listing or a save omits a secret, the
  stored one is reused only if the URL is unchanged. A new URL requires the secret
  again (`code` = `secret_required`), so a stolen admin session cannot send the stored
  keys to a host of its choice.
- **Redirects** are not followed to another host.

### 8. Supply chain

- Dependencies are locked with hashes (`uv.lock`, `package-lock.json` for the app and
  the console) and updated through Renovate. The console's Node toolchain only runs
  in the image's build stage; the runtime image has no Node.
- CI runs `pip-audit`, `npm audit` and CodeQL. GitHub Actions are pinned by commit SHA.
- Container images are built in CI, signed with cosign (keyless), and published with
  an SBOM and a Trivy scan. The runtime image is minimal, runs as non-root with a
  read-only root filesystem and no added capabilities.
- The app does not depend on remote code: EAS Update channels are signed, and there
  is no in-app web content besides the YouTube player.
- No LLM gateway library (such as LiteLLM): only the providers' official SDKs, to keep
  the dependency surface small ([ADR 0005](adr/0005-adapters-and-ai-providers.md)).

## Secrets at rest

- **Encryption.** Secrets in the `settings` table are encrypted with AES-256-GCM,
  using a key read from `TINDEERR_SECRET_KEY` (or `_FILE`). If no key is supplied, one
  is generated into `data/secret.key` (mode 0600).
- **Backups.** A copy of the database is useless without the key. Back up both, but
  separately.
- **Signing key.** The JWT signing key is derived from the same key material with its
  own label, so rotating the key signs everyone out.

## Logging

- **Structured JSON logs.** Tokens, keys, passwords, `Authorization` and `Cookie`
  headers, CSRF tokens, pairing codes, Quick Connect secrets and provider error
  bodies are redacted by a logging filter covered by tests.
- **User content.** Titles and votes are logged only at debug level.

## Privacy

- **What leaves the server.** Only these outbound calls: the configured AI provider
  (titles from votes, history and profile, plus the mood), TMDb and OMDb (title
  lookups), the media server and the request backend. The app says which AI provider
  is used before the first batch.
- **No tracking.** No telemetry and no analytics.
- **Data control.** Users can reset their votes and delete their account's Tindeerr
  data.

## Reporting a vulnerability

Report it privately through GitHub security advisories on the repository. Please do
not open a public issue.
