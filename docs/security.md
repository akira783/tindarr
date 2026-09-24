# Security model

Targets: OWASP ASVS 4 level 2 for the server and the web console, OWASP MASVS L1 for
the app.

## Assets

| Asset | Where | Impact if leaked or abused |
|---|---|---|
| AI provider API keys | server DB (encrypted) | Direct cost for the owner |
| Media server admin API key | server DB (encrypted) | Full control of the media server |
| Plex account token of the owner | server DB (encrypted) | Full control of the **plex.tv account**: every server it owns, its settings, its sharing, its devices ([ADR 0012](adr/0012-plex-tokens.md)) |
| Request backend API key | server DB (encrypted) | Unlimited requests, disk fill, quota bypass |
| TMDb / OMDb keys | server DB (encrypted) | Key revoked by the provider |
| Watch history, votes, taste profile | server DB | Privacy |
| Access / refresh tokens | app secure storage, hashed on server | Account takeover |
| Web session cookie | browser (`HttpOnly`), hashed on server | Account takeover; admin rights for an admin |
| Setup code, setup session | `data/setup-code` (never logged); cookie | Claim of an unconfigured server |
| Pairing codes | QR code on screen, hashed on server, 5 min | Account takeover (only after console approval) |
| Plex PIN and Quick Connect handles | client memory, server memory | Someone else's sign-in, or the Plex owner token |
| Media server passwords | never stored | Account takeover on the media server |
| Media server accounts' availability | media server | Lockout of real users, admins included |

## Adversaries and mitigations

### 1. Internet attacker against an exposed server

The exact rules and numbers for this section are in
[the authentication reference](auth.md).

- **Spoofed client address.** Behind a reverse proxy, every request comes from the
  proxy's private address. `X-Forwarded-For` and `X-Forwarded-Proto` are honoured only
  from `TINDARR_TRUSTED_PROXIES`, and the client IP is the rightmost untrusted hop, so a
  client cannot pick its own address to escape rate limits, look "private" or fake
  HTTPS. That setting names the proxy's own address: anything listed there can claim
  to be any client on any scheme, so a whole Docker bridge range must not be. Without that setting behind a proxy, all clients share the proxy's address:
  limits get stricter, never looser, and the server logs a hint. "Private network" is
  decided on the resolved client IP, from IP ranges only.
- **DNS rebinding.** A web page on an attacker's domain that resolves to the server's
  LAN address would reach the console with the attacker's name in `Host`. Every request
  must carry an allowed `Host` (IP literal, `localhost`, `TINDARR_ALLOWED_HOSTS`, the
  host of `public_url`), otherwise `host_not_allowed`; the expected `Origin` is built from
  that validated host.
- **Unclaimed fresh install.** The server starts in setup mode and writes a one-time
  **setup code** to `data/setup-code` (mode 0600). Its logs only give the file's path,
  never the code, so logs shipped to a log viewer do not leak it. The code is random,
  60 bits, and claims are limited per IP (plus a global slowdown), so it cannot be
  brute-forced. The console exchanges it for a setup session (its own cookie,
  30 minutes). **There is only one active setup session:** a new claim revokes the
  previous one. Setup completes only when a media server administrator signs in from
  the browser holding that setup session; the setup session is then revoked and a new
  web session issued. The code stops working at completion
  ([ADR 0011](adr/0011-hardening-after-the-pre-step-2-review.md)). The app never
  handles the setup code.
- **Credential stuffing.** Failed password sign-ins are limited per client IP, with an
  exponential pause. Errors are identical for an unknown user and a wrong password.
- **Locking real users out through Tindarr.** Jellyfin disables an account after a few
  failed logins, and failures sent through Tindarr count, even for a Jellyfin server
  that is only reachable on the LAN. Tindarr forwards at most 2 failures per
  case-folded username per 15 minutes (below Jellyfin's default of 3), then pauses that
  username without contacting the media server. The pause ends on its own: it is never
  a lock an attacker can hold on the admin. Jellyfin's counter only resets on a
  successful login, so a patient attacker can still lock an account over hours; an
  admin can therefore restrict password sign-in to the LAN or turn it off
  (`password_sign_in`), leaving Quick Connect, Plex PIN and pairing.
- **Remote-access bypass.** A Jellyfin or Emby user may be barred from remote access,
  but the media server only sees Tindarr's LAN address. Tindarr enforces the policy
  itself: such a user is refused whenever the resolved client IP is not private, at
  sign-in and on every request.
- **Token theft in transit.** HTTPS is expected. Plain HTTP is accepted by the app only
  for private IP literals, `localhost` and `.local` names, after an explicit warning.
  The console refuses to sign in over plain HTTP (`https_required`), except on
  `localhost` from the same machine, or when the operator sets
  `TINDARR_ALLOW_HTTP_CONSOLE=true`, which only applies to private client IPs and is
  logged at startup.
- **Forged or replayed tokens.** Access tokens are JWTs (HS256 with a dedicated derived
  key, required `typ`, `kid`, `iss` and `aud`), valid 15 minutes and carrying a session
  id. Refresh tokens are opaque (256 bits) and stored as SHA-256 hashes. They rotate on
  every use through an atomic compare-and-set: presenting a refresh token that was
  already used revokes the whole session (reuse detection). There is no grace period,
  even for a reuse a second after rotation. The app must therefore refresh
  single-flight; this is tested in the app
  ([ADR 0010](adr/0010-roles-and-refresh-tokens.md)).
- **Stale access.** Every authenticated request loads its session and its user, so a
  revoked session or a disabled user stops working at once. Sessions have absolute
  lifetimes (app 90 days, web 7 days). An hourly sync with the media server disables
  users removed or disabled there and removes the admin flag from users who lost it
  there.
- **Surface.** Only `/healthz`, `/api/v1/server/info`, the sign-in endpoints
  (password, Plex PIN, Quick Connect, for the app and the console), the pairing
  preview, request and completion, token refresh and the setup claim are public, plus
  the console's static files. `server/info` returns no user data and no internal URLs.
  Every public endpoint is rate-limited per client IP. Global thresholds only slow
  requests down, so an attacker cannot block legitimate use. The memory cap on
  outstanding sign-in handles is the one fixed resource: it is held by taking the slot
  from whichever client holds the most, never by refusing the household — on a Plex
  server the PIN is the only way in, so a full table had to stop being a lockout. The
  per-username password cap is the one limit that does refuse a name it has no room
  for, deliberately: it protects the media server's own lockout counter, and a live
  bucket is never evicted to make space.
- **Plex PIN and Quick Connect hijacking.** The `pin_id` or `handle` a client polls
  with is a random server-side handle (128 bits), not the plex.tv PIN id or the
  Jellyfin Quick Connect secret. It is bound to its purpose (sign-in, re-authentication
  or owner token) and to whoever started it: a pre-auth cookie for the console, a PKCE
  verifier for the app, the session for the others. A leaked handle is useless to
  anyone else.
- **Plex resource spoofing.** plex.tv lists servers as they describe themselves. The
  configured server is identified by the `machineIdentifier` read from its own
  `/identity` at setup; a sign-in is accepted only if the user's plex.tv resources
  include that exact identifier, and admin only if `owned` on it. Users are keyed by
  plex.tv account id, never by name or email. The owner token must come from the account
  that owns that server.
- **Pairing code brute force.** Pairing codes have 128 bits, live 5 minutes, work once
  and are stored hashed; the preview, request and completion are rate-limited per IP.
  Unknown, used, expired and revoked codes get the same answer (`pairing_expired`), and
  the preview only returns what the app must display.
- **Exposure.** Docker's published ports bypass host firewalls such as ufw. The example
  compose file publishes the port on `127.0.0.1` only, for a reverse proxy on the same
  host; publishing it on the LAN is a deliberate change.

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
  admin. A **file import** is bounded four ways: five uploads an hour per client, eight
  megabytes per upload (checked against the declared length *and* against the bytes as
  they arrive), two hundred thousand rows and two thousand distinct titles per file, and
  one running import per user with two for the whole server. An archive is refused on
  its own declared sizes before a member is read and again on the running total as it
  inflates, so a zip bomb never reaches a parser.
- **Imported history.** An import is a file somebody else's computer wrote, and every
  row it produces is keyed on the uploader: `watch_history`, `imports` and
  `import_reviews` are read and written through queries scoped to the session's user,
  so another user's import id or review-entry id is a `404`. There is no shared
  catalogue to corrupt — a row is a TMDb id and a verdict about one household's
  evening — and a review entry can only be accepted as one of the candidates it itself
  offered, so the endpoint is an answer and not a writer.
- **Admin rights.** A user is admin when the media server says so at their last
  sign-in (re-read at every sign-in) or when a Tindarr admin promoted them
  ([ADR 0010](adr/0010-roles-and-refresh-tokens.md)). Someone removed as administrator
  on the media server loses Tindarr admin within the hour (sync) or at their next
  sign-in, unless promoted here. A demotion done only in Tindarr lasts until that media server
  administrator's next sign-in, and only a media server administrator can demote or
  disable another one, so a promoted admin cannot lock them out. The last enabled
  admin cannot be demoted or disabled. The role is checked in the database on every
  admin request, so a change applies at once. Only admins can see settings (secrets
  are masked: `set` + last 4 characters, never the value), change connectors or run
  connection tests, and only from the web console: a bearer token from a phone is
  refused on admin endpoints.
- **Repointing the media server.** Whoever controls the media server connector sees
  every user's password at their next sign-in, and decides who is a media server
  administrator. Only a media server administrator can change it, and only after
  re-authenticating on the **current** media server in the last 5 minutes, so neither a
  promoted admin nor a stolen admin cookie can do it. Credentials are never sent to the
  new URL before it is saved. If the new server is a different one (its id or Plex
  `machineIdentifier` differs), every session is revoked and every user unlinked. If
  the configured address starts answering with another identity, sign-ins stop until an
  admin or the operator acts.

### 3. Stolen or lost phone

- Tokens live in the Android Keystore through `expo-secure-store`. They are never
  written to logs, crash reports or backups (`allowBackup=false`).
- Each device is a separate session. Users can list their sessions from the app or the
  console, and revoke them from the console; admins can revoke all of a user's
  sessions.
- A phone's token cannot reach admin, pairing or account-deletion endpoints, nor revoke
  other sessions, so a stolen phone cannot change connectors, pair another device,
  delete the owner's data or sign the owner out of the console.
- Refresh tokens expire after 60 days of inactivity, and every app session after
  90 days.

### 4. Attacks through the browser against the web console

- **Session theft by script (XSS).** The session token is only in an `HttpOnly`
  cookie; no token is kept in `localStorage` or readable by JavaScript. The console
  renders server data as text through React, never as HTML. Its
  Content-Security-Policy is strict: `default-src 'self'`, `script-src 'self'` (no
  inline script, no `eval`), `img-src 'self' https://image.tmdb.org`,
  `connect-src 'self'`, `object-src 'none'`, `base-uri 'none'`,
  `form-action 'self'`, `frame-ancestors 'none'`,
  `require-trusted-types-for 'script'`. No third-party script or font.
- **Cookie.** `__Host-tindarr_session`: `Secure`, `HttpOnly`, `SameSite=Strict`,
  `Path=/`, no `Domain`, so a sibling subdomain cannot set or read it. It holds an
  opaque 256-bit token stored hashed. Web sessions expire after 24 h idle and 7 days
  after sign-in. The setup session uses a separate cookie, `__Host-tindarr_setup`, and
  console sign-in handles a pre-auth cookie, `__Host-tindarr_preauth`, with the same
  flags.
- **CSRF.** Three layers: `SameSite=Strict`; a CSRF token bound to the server-side
  session, sent as `X-CSRF-Token` on every `POST`, `PUT`, `PATCH` and `DELETE` and
  compared in constant time; and an `Origin` check (the server's own origin, built
  from an allowed `Host`; a missing `Origin` is refused). The web sign-in endpoints and the
  setup claim check `Origin` too, against login CSRF. Requests authenticated by a
  bearer header skip the CSRF check (a browser never adds that header by itself),
  and then every cookie is ignored; a console endpoint then answers `401`. CORS is
  disabled.
- **Clickjacking.** `frame-ancestors 'none'` and `X-Frame-Options: DENY`.
- **Caching.** API responses are `no-store`; so is `index.html`. Only hashed static
  assets under `/assets/` are cached (immutable).
- **Libraries under the CSP.** The console may not inject `<style>` elements or use
  `innerHTML` (Trusted Types). The QR code is rendered as React SVG elements or on a
  canvas, and an end-to-end test fails on any CSP violation.

### 5. Phone pairing, Plex PIN and Quick Connect

- **Photo or shoulder-surfing of the QR code.** A code alone does not sign anyone in.
  When a phone asks to pair, the console shows its name, platform, IP address and a
  4-digit confirmation code that the real phone also displays, and tokens are issued
  only after the user clicks Approve. Someone who photographs the QR code and pairs
  first shows up as an unexpected request, and the user's own phone gets
  "code already used" instead of a matching confirmation code. The code also lives
  5 minutes and works once, and the session appears in the user's session list.
- **Phishing a victim into scanning an attacker's QR code.** The victim's app would be
  signed in to the attacker's server, as the attacker's user, and could send it votes
  and moods. Before accepting, the app shows the target host prominently (in punycode
  for internationalised names) and "You will be signed in as <name>" (from
  `POST /auth/pair/preview`), asks for confirmation, and warns when it would replace an
  existing connection. It refuses an `http` server URL unless the host is a private IP
  literal, `localhost` or `.local`, decided without any DNS lookup.
- **Another app catching the link.** Any Android app can register the `tindarr://`
  scheme, so a code opened from the system camera could be intercepted. The app's own
  scanner is the main path, and an intercepted code still needs the console approval.
- **`public_url` abuse.** The QR link carries `public_url`. An admin session pointing it
  at another host would send pairing codes there. Only a media server administrator with
  a fresh re-authentication can change it, the console writes the host next to every QR
  code, and the value is verified before it is saved: the server fetches its own
  `server/info` through that URL and checks an HMAC of a fresh nonce **bound to the host
  the request arrives at**. The host must therefore already be one this server answers
  to, which is what stops an address that can merely *reach* the server from relaying a
  proof for itself. A reverse proxy that forwards to this same instance still passes, by
  design ([the reference](auth.md#10-public_url) has the full argument, including the
  residual case of an IP-literal address).
- **No short typed code.** Pairing is QR-only, so there is no low-entropy code on the
  public surface; users who cannot scan sign in normally.
- **Quick Connect phishing.** An attacker could start a Quick Connect sign-in and
  convince a victim to approve the code in their Jellyfin client. This is inherent to
  Quick Connect (Jellyfin's own clients have the same risk). The code expires, the
  handle works once for whoever started it, and the session list shows the new
  session. Admins can leave Quick Connect disabled in Jellyfin, which removes the
  method.
- **Plex PIN phishing.** The same attack with a Plex PIN: an attacker starts a sign-in
  and sends the victim the `app.plex.tv` link. Binding the handle does not help, since
  the attacker is the initiator. The plex.tv approval page names the device
  "Tindarr (<server name>)", the PIN expires, and the new session shows in the
  victim's session list. An attacker cannot get the owner token this way: `owner_token`
  PINs can only be started from the setup session or by a media server administrator,
  and the token must come from the account that owns the configured server.
- **Leftover media server sessions.** Tindarr ends the Jellyfin or Emby session opened
  by each sign-in, cleans up Quick Connect approvals nobody collected, and deletes the
  plex.tv device created by each Plex sign-in (an unofficial plex.tv endpoint; when it
  fails, the device stays listed in the user's plex.tv account and can be removed
  there). Tindarr never stores a user's media server token.

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
  stored one is reused only if the address is unchanged. A new address requires the
  secret again (`code` = `secret_required`), so a stolen admin session cannot send the
  stored keys to a host of its choice. "The address" is read strictly: for the AI
  provider it includes the **provider** (switching from a local gateway to a hosted one
  is a new address), and for the media server and the request backend it includes
  `verify_tls`, since turning verification off while omitting the key would replay that
  key over a connection nobody checks. TMDb and OMDb have one host each, written into
  Tindarr, so their keys have no address to be moved to.
- **A pinned secret pins its address.** A secret set by an environment variable obeys
  the same rule, and this is the part that is easy to get backwards: it cannot be
  changed from the console, so it is tempting to treat it as always available. It is a
  stored secret, and an administrator cannot "send it again" — they may not set it at
  all. So once a connector has an address, that address is fixed with the secret, and
  moving it answers `setting_locked` naming the variable to change instead. A connector
  whose key is pinned cannot be removed either: an unconfigured connector accepts any
  first address, which would make removal the way around the rule.
- **Redirects** are not followed to another host. The `public_url` check follows none.
- **Bodies are bounded as they arrive.** Every call to an address an administrator typed
  stops reading at the size limit rather than after it, so a hostile or broken endpoint
  cannot grow the process before the check runs. A connection test's answer carries at
  most a coarse health value plus the remote product's own name and version, each cut to
  64 printable characters; a model listing carries model ids only, capped in number and
  in length.

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
  using a key read from `TINDARR_SECRET_KEY` (or `_FILE`). If no key is supplied, one
  is generated into `data/secret.key` (mode 0600).
- **Backups.** A copy of the database is useless without the key. Back up both, but
  separately.
- **Signing key.** The JWT signing key is derived from the same key material with its
  own label (`tindarr/v1/jwt-signing`), so rotating the key signs everyone out. The
  `public_url` proof uses another label (`tindarr/v1/public-url-proof`).

## Logging

- **Structured JSON logs.** Tokens, keys, passwords, `Authorization` and `Cookie`
  headers, CSRF tokens, setup codes, pairing codes, PKCE verifiers, refresh tokens,
  Plex PIN and Quick Connect handles, Quick Connect secrets and provider error bodies
  are redacted by a logging filter covered by tests. Any value that is not a plain
  string or number is converted to text and redacted before it is written.
- **No credentials in URLs.** Tindarr's own endpoints take codes, handles and tokens in
  bodies, cookies or headers, never in a path or query string. Plex tokens go in the
  `X-Plex-Token` header. The outbound HTTP client's own logger stays at `WARNING`, so
  URLs such as Quick Connect's `?secret=` are never logged.
- **Client-supplied request ids** are only kept from trusted proxies.
- **Security events** (sign-ins, refresh-token reuse, revocations, pairing approvals,
  connector and `public_url` changes, identity mismatches, rate-limit trips) are logged
  without secrets.
- **User content.** Titles and votes are logged only at debug level.

## Privacy

- **What leaves the server.** Only these outbound calls: the configured AI provider
  (titles from votes, history and profile, plus the mood), TMDb and OMDb (title
  lookups), the media server and the request backend. The app says which AI provider
  is used before the first batch.
- **No tracking.** No telemetry and no analytics.
- **Data control.** Users can reset their votes (app or console) and delete their
  account's Tindarr data (console). An import can be forgotten on its own, which deletes
  everything that source told the server about that user and leaves the others standing.
- **What an import keeps.** The uploaded file is read in the request that carried it and
  is never written to disk; its **name is never sent**, because the body is the file and
  not a form. What is stored is the titles that could be identified, the rows that could
  not (so somebody can come back and answer them), and counts. A failure is recorded as
  a problem code — never a parser's words and never a line of the file — and no log line
  carries a title, a query, a file name or a user id.

## Reporting a vulnerability

Report it privately through GitHub security advisories on the repository. Please do
not open a public issue.
