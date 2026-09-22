# Security model

Targets: OWASP ASVS 4 level 2 for the server, OWASP MASVS L1 for the app.

## Assets

| Asset | Where | Impact if leaked or abused |
|---|---|---|
| AI provider API keys | server DB (encrypted) | Direct cost for the owner |
| Media server admin API key / Plex owner token | server DB (encrypted) | Full control of the media server |
| Request backend API key | server DB (encrypted) | Unlimited requests, disk fill, quota bypass |
| TMDb / OMDb keys | server DB (encrypted) | Key revoked by the provider |
| Watch history, votes, taste profile | server DB | Privacy |
| Access / refresh tokens | app secure storage, hashed on server | Account takeover |
| Media server passwords | never stored | Account takeover on the media server |

## Adversaries and mitigations

### 1. Internet attacker against an exposed server

- **Unclaimed fresh install.** The server starts in setup mode and prints a one-time
  **setup code** to its logs, and writes it to `data/setup-code` with mode 0600. The
  code is random, at least 60 bits (12 base32 characters), and claim attempts are
  rate-limited per IP and globally, so it cannot be brute-forced. Every setup endpoint
  requires that code (or the setup token it is exchanged for), so the first stranger to
  find the URL cannot claim the instance. The code expires as soon as setup completes
  ([ADR 0004](adr/0004-authentication.md)).
- **Credential stuffing.** The login rate limit applies per IP and per username, with
  exponential backoff. Errors are identical for an unknown user and a wrong password.
  The media server enforces its own lockout too.
- **Token theft in transit.** HTTPS is expected. Plain HTTP is accepted by the app only
  for private and link-local addresses and `.local` names, after an explicit warning.
- **Forged or replayed tokens.** Access tokens are JWTs, short-lived (15 min), signed
  with a server key and carrying a session id. Refresh tokens are opaque (256 bits) and
  stored as SHA-256 hashes. They rotate on every use: presenting a refresh token that
  was already used revokes the whole session (reuse detection).
- **Surface.** Only `/healthz`, `/api/v1/server/info`, the sign-in endpoints
  (password, Plex PIN), token refresh and the setup claim are public. `server/info`
  returns no user data and no internal URLs.
- **Plex PIN hijacking.** The `pin_id` the app polls with is a random server-side
  handle (at least 128 bits), not the plex.tv PIN id, and it works once. Guessing it to
  catch someone else's sign-in is not practical.

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
- **Admin rights.** Media server administrators become Tindeerr admins; the admin can
  promote or demote other users. Only admins can see settings (secrets are masked:
  `set` + last 4 characters, never the value), change connectors or run connection
  tests.

### 3. Stolen or lost phone

- Tokens live in the Android Keystore through `expo-secure-store`. They are never
  written to logs, crash reports or backups (`allowBackup=false`).
- Each device is a separate session. Users can list and revoke their sessions, and
  admins can revoke all of a user's sessions.
- Refresh tokens expire after 60 days of inactivity.

### 4. Untrusted data from external systems (LLM, TMDb, media server)

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

### 5. Admin-configured URLs (SSRF)

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

### 6. Supply chain

- Dependencies are locked with hashes (`uv.lock`, `package-lock.json`) and updated
  through Renovate.
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

- **Structured JSON logs.** Tokens, keys, passwords, `Authorization` headers and
  provider error bodies are redacted by a logging filter covered by tests.
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
