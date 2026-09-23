# Authentication, sessions and setup: reference

This is the normative description of what step 2 implements: the network rules the
server applies to every request, first-run setup, sign-in through the media server,
sessions and tokens, phone pairing, rate limits and the tables behind them. The
reasons for these choices are in [ADR 0004](adr/0004-authentication.md),
[ADR 0009](adr/0009-web-console-and-phone-pairing.md),
[ADR 0010](adr/0010-roles-and-refresh-tokens.md) and
[ADR 0011](adr/0011-hardening-after-the-pre-step-2-review.md); the threats they answer
are in the [security model](security.md). The HTTP details (fields, status codes,
problem `code` values) are in [`api/openapi.yaml`](../api/openapi.yaml). When this file
and the contract disagree, that is a bug: fix one of them before implementing.

## 1. Request context

Every HTTP request is first resolved into a small context: client IP, effective
scheme, host and origin. Everything else (rate limits, "private network" decisions,
cookie flags, CSRF) uses this context and never reads the raw headers again.

### Client IP and scheme

- `TINDARR_TRUSTED_PROXIES` is a comma-separated list of IPs or CIDRs (bootstrap
  configuration, empty by default). It is the existing `trusted_proxies` setting of
  `ServerConfig`. **List the proxy's own address, not the network it sits on.**
  Everything in that list may claim any client address — `127.0.0.1` included, which
  is both private and loopback — and any scheme, which is what `remote_access_denied`,
  `password_sign_in: lan_only` and `https_required` are decided on. A range such as
  `172.16.0.0/12` hands that to every container on every Docker bridge.
- **Peer not trusted** (or the list is empty): the client IP is the TCP peer address.
  `X-Forwarded-For`, `X-Forwarded-Proto`, `Forwarded` and `X-Request-ID` are ignored.
  When such a header arrives from a peer in a private range, the server logs one
  warning per peer and per hour ("forwarded headers from an untrusted peer: add it to
  TINDARR_TRUSTED_PROXIES?"), without the header values.
- **Peer trusted:** the client IP is the **rightmost untrusted hop**: walk
  `X-Forwarded-For` from right to left, skip every address inside
  `TINDARR_TRUSTED_PROXIES`, and take the first one outside it. If every hop is
  trusted, take the leftmost. If the hop reached is not a valid IP address (a
  misconfigured proxy), the client IP is the peer itself and a warning is logged.
- **Scheme:** the scheme of the connection, replaced by `X-Forwarded-Proto` only when
  the peer is trusted (last value when the header is repeated; only `http` and `https`
  are accepted, anything else is ignored).
- `X-Forwarded-Host` and `X-Forwarded-Port` are always ignored. The reverse proxy must
  pass the original `Host` header (Traefik and Caddy do by default; nginx needs
  `proxy_set_header Host $host`).
- **Normalisation:** addresses are parsed with `ipaddress.ip_address`; IPv4-mapped
  IPv6 addresses (`::ffff:a.b.c.d`) become IPv4. Rate limits key IPv4 by address and
  IPv6 by its /64 network.
- All of this is one helper (`client_ip`, `effective_scheme`) with its own tests:
  spoofed headers from an untrusted peer, garbage and IPv6 entries, several proxies.

### Private network

A **private client IP** is a resolved client IP inside `127.0.0.0/8`, `::1/128`,
`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, `fc00::/7` or
`fe80::/10`. Nothing else: no DNS lookup, and `100.64.0.0/10` (carrier-grade NAT, also
used by Tailscale) is not private. The decision is always made on the resolved client
IP, never on the TCP peer (behind a reverse proxy the peer is always private).

It is used for three things: the remote-access rule (section 4), the
`password_sign_in = lan_only` setting (section 4), and plain-HTTP console access
(below).

### Allowed hosts (DNS rebinding)

The `Host` header of every request except `/healthz` must be one of:

- an IP literal (IPv4, or IPv6 in brackets), with any port;
- `localhost`, with any port;
- a host name listed in `TINDARR_ALLOWED_HOSTS` (bootstrap configuration,
  comma-separated host names, compared case-insensitively, any port);
- the host of `public_url`, once it is set.

Nothing else, ever — not even for the length of a `public_url` check (section 10).
Anything else gets `400` with `code` = `host_not_allowed`, before routing, for the API
and the console alike. A server reached by a domain name must therefore have
`TINDARR_PUBLIC_URL` or `TINDARR_ALLOWED_HOSTS` set before its first start, or be set
up through its IP address. Setting `public_url` to a name the server does not already
answer to is refused for the same reason (section 10).

### Origin

The **server's origin** for a request is `<effective scheme>://<Host header>`. The
`Host` has already passed the allowed-hosts check, so the origin always comes from the
configuration, never from an arbitrary header. `public_url` is not a second accepted
origin: the console must be used at the origin it was loaded from.

## 2. Credentials and how a request is authenticated

| Credential | Carried as | Kind | Accepted by |
|---|---|---|---|
| Access token | `Authorization: Bearer <JWT>` | `mobile` session | Shared endpoints: `GET /me`, `GET /me/sessions`, `/swipe/…`, `POST /auth/logout` |
| Web session | cookie `__Host-tindarr_session` | `web` session | Shared endpoints and console endpoints |
| Setup session | cookie `__Host-tindarr_setup` | `setup` session | Setup endpoints, `GET /auth/web/session`, the web sign-ins while setup is pending, Plex PINs with purpose `owner_token` during setup (`POST /auth/plex/pins`, `POST /auth/plex/pins/status`) |
| Pre-auth cookie | cookie `__Host-tindarr_preauth` | none | Binds console sign-in handles to the browser (section 5) |

Rules, applied in this order:

1. If an `Authorization` header is present, it is the only credential considered:
   every cookie is ignored and no CSRF check applies (a browser never adds that header
   by itself). An endpoint that does not accept bearer tokens then answers `401`
   `unauthorized`, exactly as if no credential had been sent.
2. Otherwise the cookie matching the endpoint's accepted kind is looked up. A cookie of
   another kind is never accepted in its place (a setup session cannot reach `/me`, a
   web session cannot reach `/setup/…`).
3. **Every authenticated request loads the session row and the user row.** The request
   fails with `401` `unauthorized` when the session is revoked, idle-expired or past its
   absolute lifetime, when the user is disabled or unlinked, or when a JWT's `sid` does
   not match a live `mobile` session. A revocation or a disable therefore takes effect
   on the next request, not when the access token expires.
4. **CSRF** on `POST`, `PUT`, `PATCH` and `DELETE` authenticated by a cookie: the
   `X-CSRF-Token` header must equal the session's CSRF token (constant-time
   comparison), and `Origin` must equal the server's origin; a missing `Origin` is
   refused. Failure: `403` `csrf_failed`. `GET` and `HEAD` are never checked and never
   change state.
5. **Login CSRF:** endpoints that set a cookie without a session (`POST /setup/claim`,
   the `POST /auth/web/…` sign-ins, `POST /auth/plex/pins` and
   `POST /auth/quick-connect` when called by the console) check `Origin` the same way.
6. **Role:** console endpoints tagged `admin` need the effective role `admin`
   (`media_server_admin` or `promoted`), read from the user row on every request;
   otherwise `403` `admin_required`. Some actions need more (section 7).
7. **Remote access:** a user whose `remote_access` flag is false gets `403`
   `remote_access_denied` on every request whose client IP is not private (section 4).

### Cookies

| Name | Value | Attributes | Lifetime |
|---|---|---|---|
| `__Host-tindarr_session` | 256-bit random, base64url; SHA-256 stored | `Secure; HttpOnly; SameSite=Strict; Path=/` | session cookie (no `Max-Age`); server-side 24 h idle, 7 days absolute |
| `__Host-tindarr_setup` | same | same | server-side 30 min absolute |
| `__Host-tindarr_preauth` | same; SHA-256 kept in the handle | same, `Max-Age=900` | 15 min; cleared by a successful web sign-in |

**Plain HTTP.** Cookie-setting endpoints answer `403` `https_required` unless one of
these holds:

- the effective scheme is `https`;
- the client IP is loopback and the `Host` is `localhost`, `127.0.0.1` or `[::1]`
  (browsers treat these as secure contexts, so the `__Host-` cookies still work);
- `TINDARR_ALLOW_HTTP_CONSOLE=true` (bootstrap configuration, off by default, logged as
  a warning at startup) **and** the client IP is private. Only then are the cookies
  named `tindarr_session`, `tindarr_setup` and `tindarr_preauth`, without `Secure`.
  The server reads the unprefixed names only on such requests.

## 3. First-run setup

**Setup code.**

- At startup, when setup is not completed and `server_state.setup_code_hash` is empty,
  the server generates a code: 60 random bits as 12 Crockford base32 characters. It
  writes the code to `<data>/setup-code` (mode 0600, created with `O_EXCL`) and its
  SHA-256 to `server_state.setup_code_hash`.
- The log line gives **only the path**: "setup required: enter the code from
  /data/setup-code in the web console". The code itself is never logged.
- On restart before completion, the same code stays valid. Deleting
  `<data>/setup-code` and restarting generates a new one (the hash is replaced when
  the file is missing).
- The code can be used any number of times until setup completes, within the claim
  rate limits (section 8). Setup completion deletes the file and clears the hash.

**Claim.** `POST /setup/claim` with the code (compared by hash, constant time). It
creates a `setup` session (30 minutes, absolute, no user, its own CSRF token), sets
`__Host-tindarr_setup`, and **revokes every earlier setup session**: there is only
one active setup session at a time, and the newest claim wins. After completion the
endpoint answers `409` `setup_completed`.

**Setup state.** `GET /setup/state` (setup session) tells the wizard what is left:
whether the media server is configured, whether environment variables lock it, and
which sign-in methods the configured server offers.

**Media server.** `PUT /setup/media-server` (setup session + CSRF) tests and saves the
connection (section 6). When `TINDARR_MEDIA_SERVER_KIND`, `_URL` and `_API_KEY` are all
set, the wizard skips this step; a request that gives a locked field a different
value answers `409` `setting_locked`. For Plex, the owner token comes from a Plex PIN
created with purpose `owner_token` under the setup session (section 5), whose status
the wizard polls with `POST /auth/plex/pins/status`.

**Completion.** Setup completes at the first successful **console** sign-in (password,
Plex PIN or Quick Connect) that meets all of these:

- the request carries the current setup session cookie (otherwise `403`
  `setup_session_required`), so only the browser that claimed can complete;
- a media server is configured;
- the user is administrator on the media server (otherwise `403` `admin_required`).

In one transaction the server then marks setup completed, creates the user, revokes the
setup session, clears the setup code hash, and opens a **new** web session with a new
token and CSRF token (no session fixation). The response clears the setup cookie, sets
the session cookie and says `setup_completed_now: true`. The code file is deleted
after the commit. The app's sign-in endpoints answer `503` `setup_required` until then.

**Restart during setup.** Setup sessions are rows in `sessions`, so a claim survives a
restart until its 30 minutes run out. Plex PINs and Quick Connect handles live in
memory (section 5) and are lost: the wizard starts a new PIN.

**Reset.** `tindarr media-server reset` (CLI, run inside the container, so it needs
host access) clears the media server settings and its identity, revokes every
session, unlinks every user, clears `setup_completed_at` and generates a new setup
code. It is the recovery path when the media server was reinstalled or replaced
(section 6).

## 4. Sign-in through the media server

Sign-in works the same for the app and the console; only the result differs (a token
pair and a `mobile` session, or a cookie and a `web` session). After a successful
check the server:

1. finds the user by `media_server_user_id` (normalised, below), or creates it;
2. refuses a user disabled in Tindarr (`403` `account_disabled`), except one disabled
   only because the media server had disabled or removed them
   (`disabled_reason = media_server`), who is re-enabled since the media server now
   accepts them;
3. writes `name`, `media_server_admin` and `remote_access` from the media server, sets
   `last_sign_in_at`;
4. applies the remote-access rule, then opens the session.

**User ids.** Jellyfin and Emby ids are stored lowercase, 32 hex digits, dashes
removed. Plex users are keyed by their plex.tv account id (decimal string from
`GET https://plex.tv/api/v2/user`), never by user name or email.

**Remote access.** Jellyfin and Emby enforce `Policy.EnableRemoteAccess` against the
caller's IP, and for them the caller is Tindarr, usually on the LAN. Tindarr applies
the rule itself: a user whose policy says `EnableRemoteAccess = false` is refused
(`403` `remote_access_denied`) when the client IP is not private, at sign-in and on
every later request (section 2). Plex users always have `remote_access = true`.
Jellyfin's parental access schedules are not enforced by Tindarr.

### Media server client identity

Every call to Jellyfin or Emby carries:

```
Authorization: MediaBrowser Client="Tindarr", Device="Tindarr server",
  DeviceId="<server_state.install_id>", Version="1", Token="<token>"
```

- One header for both servers and every Jellyfin version supported. Jellyfin 12
  ignores `X-Emby-Token`, `X-Emby-Authorization`, the `Emby` scheme and `?api_key=`, so
  they are never used.
- `Token` is the admin API key for admin calls, the user's fresh token for
  `POST /Sessions/Logout`, and absent for `AuthenticateByName` and Quick Connect calls.
- `DeviceId` is stable per install (generated with the install id). `Version` is a
  constant, not the Tindarr version: Emby identifies a device by all four values,
  and Jellyfin revokes a user's older tokens with the same `DeviceId` at each login,
  which also cleans up any leftover Tindarr session.
- A Jellyfin user limited to specific devices (`EnableAllDevices = false`) must be
  allowed the "Tindarr server" device, or the media server refuses the sign-in.
- **Supported versions:** Jellyfin 10.10 or newer (10.10, 10.11 and 12.x are tested);
  Emby: the version pinned in the end-to-end workflow, older ones best effort. The
  version and product come from `GET /System/Info/Public`; an older Jellyfin makes the
  connection test fail with `media_server_unsupported`.

Calls to plex.tv always send `X-Plex-Product: Tindarr` and an
`X-Plex-Client-Identifier`; Plex tokens always travel in the `X-Plex-Token` header,
never in a query string.

### Password (Jellyfin, Emby)

1. `password_sign_in` setting: `enabled` (default), `lan_only` (only from a private
   client IP) or `disabled`. Refused: `403` `password_sign_in_disabled`. Quick Connect,
   Plex PIN and pairing are not affected, and `password` is left out of `auth_methods`
   when it does not apply to the caller.
2. Rate limits (section 8), then `POST /Users/AuthenticateByName`
   `{"Username": …, "Pw": …}`.
3. `401` from the media server → `401` `invalid_credentials` (same answer for an
   unknown user). `403` → `403` `account_disabled` (disabled on the media server,
   session limit or device restriction).
4. Success: read `User.Id`, `User.Name`, `User.Policy.IsAdministrator`,
   `User.Policy.EnableRemoteAccess`, then call `POST /Sessions/Logout` with the returned
   `AccessToken`. The password and the token are dropped; nothing is stored.

**Lockout through Tindarr.** Jellyfin disables an account after
`LoginAttemptsBeforeLockout` failures (by default 3 for a user, 5 for an
administrator), and only a successful login resets its counter. Every failure
forwarded by Tindarr counts. So:

- per **case-folded username** (NFKC, then `str.casefold`), all clients and IPs
  together, Tindarr forwards at most **2 failures per rolling 15 minutes**. Beyond
  that it answers `429` `rate_limited` with `retry_after_ms` without calling the media
  server. It is a pause, not a lock: it ends when the oldest failure leaves the window,
  and a successful sign-in for that username clears the count;
- the same limit applies to unknown usernames, so it tells nothing about which
  accounts exist;
- this bounds the rate but cannot see failures made directly on Jellyfin, and the
  Jellyfin counter only resets on success, so a determined attacker can still lock an
  account over several windows. Servers exposed to the Internet should set
  `password_sign_in` to `lan_only` or `disabled` and use Quick Connect, Plex PIN or
  pairing from outside.

### Quick Connect (Jellyfin)

1. `POST /auth/quick-connect` → server calls `POST /QuickConnect/Initiate` (the four
   client fields, no token; `401` there means Quick Connect is off: `409`
   `quick_connect_unavailable`). It keeps `Secret`, returns `Code` and a handle.
2. The user approves the code in a Jellyfin client where they are signed in.
3. Each call to the login endpoint checks `GET /QuickConnect/Connect?secret=…` at most
   once per second (more frequent calls get `202` without an upstream call). Once
   `Authenticated` is true: `POST /Users/AuthenticateWithQuickConnect`
   `{"Secret": …}`, then the same steps as a password sign-in from step 4, including
   the logout of the Jellyfin session.
4. **Cleanup:** Jellyfin creates the session when the user approves, not when Tindarr
   collects it. A background sweep (every 30 s) takes each Quick Connect handle that
   expired or was abandoned unused, checks it once more, and if it was approved,
   authenticates and logs that session out. If the server restarted in between, the
   leftover Jellyfin session is revoked at that user's next Tindarr sign-in (same
   `DeviceId`).
5. Whether Quick Connect is on (`GET /QuickConnect/Enabled`, no token) is cached for
   5 minutes; the cached value drives `auth_methods`, so `server/info` never calls the
   media server itself.

### Plex PIN

1. `POST /auth/plex/pins` → server calls `POST https://plex.tv/api/v2/pins?strong=true`
   and `X-Plex-Device-Name` set to "Tindarr (<server name>)", so the plex.tv approval
   page names the server. The client identifier is **generated for this PIN** (random
   UUID, kept in the handle) for purpose `sign_in` and `reauth`; only an `owner_token`
   PIN uses the install's stable identifier (`install_id`), which every later call made
   with the owner token also sends. So revoking a sign-in device (step 4) can never
   revoke the owner token. The response's `expiresAt` bounds the handle.
2. The client opens `https://app.plex.tv/auth#?clientID=<id>&code=<code>&context%5Bdevice%5D%5Bproduct%5D=Tindarr`.
3. Each call to the login endpoint polls `GET https://plex.tv/api/v2/pins/{id}` with the
   same client identifier, at most once per second per handle, backing off on `429`.
4. Once the PIN has an `authToken`:
   - `GET https://plex.tv/api/v2/user` → account id and display name;
   - `GET https://plex.tv/api/v2/resources` → the resource whose `clientIdentifier` equals
     the stored `machineIdentifier` (section 6) and that provides `server`. None: `403`
     `not_a_server_user`. Resource names and advertised URLs are never used for this
     decision: they are self-reported;
   - administrator = `owned` is true on **that** resource;
   - then the token is revoked: find the device with this PIN's client identifier
     (`GET https://plex.tv/devices.xml`) and delete it
     (`DELETE https://plex.tv/devices/{id}.xml`), so no live "Tindarr" device stays in
     the user's plex.tv account. These two calls are what python-plexapi uses; they are
     not in Plex's official documentation. If they fail, the sign-in still succeeds,
     a warning is logged (without the token), and the device stays listed in the
     user's plex.tv "Authorized devices", where they can remove it. Tindarr keeps no
     copy of that token either way.
5. plex.tv unreachable: `503` `plex_tv_unreachable`.

- Tindarr never registers a JWK with plex.tv (the newer JWT flow): doing so with the
  owner token would expire that token. The classic PIN flow is not deprecated.
- **Limitation:** managed Plex Home profiles cannot use the PIN flow, so they cannot
  sign in to Tindarr.

## 5. Handles (Plex PINs and Quick Connect)

Clients never see the plex.tv PIN id or the Jellyfin secret. They get a **handle**:
128 random bits, base64url. Handles live **in memory only**, in one process-wide
registry; a restart forgets them and clients get `410` (`pin_expired` or
`quick_connect_expired`) and start again.

Each handle records:

- the **purpose**: `sign_in`, `reauth` (section 7), or `owner_token` (Plex only: the
  owner token for the media server connector);
- the **initiator binding**, checked on every use:

  | Purpose | Created by | Bound to | Used by |
  |---|---|---|---|
  | `sign_in` (app) | the request carries `code_challenge` | `code_challenge` = base64url(SHA-256(`code_verifier`)), PKCE S256 | `POST /auth/plex/login`, `POST /auth/quick-connect/login` with `code_verifier` |
  | `sign_in` (console) | no `code_challenge`; `Origin` checked | the pre-auth cookie (set if absent) | `POST /auth/web/plex/login`, `POST /auth/web/quick-connect/login` |
  | `reauth` | web session + CSRF | that session id | `POST /auth/web/reauth` |
  | `owner_token` | setup session, or web session of a media server administrator, + CSRF | that session id | `POST /auth/plex/pins/status`, `PUT /setup/media-server`, `PUT /admin/connectors/media_server` and its `test` (which does not consume it) |

- the upstream PIN id and client identifier, or the Quick Connect secret; the expiry:
  the upstream expiry, capped at 10 minutes for Plex and 5 minutes for Quick Connect
  (shorter than Jellyfin's 10, so the cleanup sweep still has time);
- once collected, the result (for `owner_token`, the token itself, in memory only).

A handle used with the wrong purpose, the wrong binding, after expiry, or after it was
consumed gets the same answer as an unknown handle (`410`). A `sign_in` or `reauth`
handle is consumed by the call that completes the sign-in. An `owner_token` handle is
consumed only when the connector is saved: a failed connection test (`502`) leaves it
usable until it expires, so the admin can fix the URL and retry.

**Caps.** At most 5 outstanding handles per client IP, and 200 in total. Beyond either,
creation answers `429` `rate_limited`. The global cap is a memory bound, and the only
global limit that refuses instead of slowing down (section 8).

## 6. Media server connector

**Identity.** The configured server's identity is stored in
`server_state.media_server_identity`: `jellyfin:<Id>` or `emby:<Id>` (normalised `Id`
from `GET /System/Info/Public`), or `plex:<machineIdentifier>` from the Plex server's own
`GET /identity`. It is read when the connector is saved, then re-read at most every
5 minutes before a sign-in uses the server, at startup, and by the hourly sync.

**Connection test** (setup, admin save, admin test):

- Jellyfin / Emby: `GET /System/Info/Public` (product must match the declared kind,
  version at least 10.10 for Jellyfin), then `GET /Users` with the API key (proves the
  key works), then `GET /System/Configuration` with it (proves the key is an
  **administrator** key).
  - `GET /Users` alone proves nothing: both products answer `200` to any authenticated
    caller and silently filter the list to what that caller may see, so a *user* access
    token pasted into the API key field would pass — and the hourly sync would then read
    that one-entry list as "everyone else was removed". `GET /System/Configuration` is
    the endpoint both gate on the administrator role (Jellyfin's
    `ConfigurationController` requires the `RequiresElevation` policy, Emby's the `Admin`
    role): `401` or `403` there means the key works but is not an admin key
    (`unauthorized`).
  - A server that answers that probe with anything else (an older or forked build
    without the route) leaves the question open: the key is accepted, the doubt is
    logged, and the sync's own guard — a list with no users at all changes nothing —
    stays the backstop.
- Plex: `GET <url>/identity` for the `machineIdentifier`, then
  `GET https://plex.tv/api/v2/resources` with the token from the `owner_token` handle:
  the resource with that `clientIdentifier` must be `owned` (otherwise `403`
  `plex_owner_required`); then `GET <url>/` with the token.
- Results are coarse (security model, section 7): `ok`, `unauthorized`, `unreachable`,
  `unexpected_response`, `unsupported_version`. No response body is ever returned.

**What the console sees of a stored secret.** `SecretState` says whether one is set and,
for a Jellyfin or Emby API key, its last four characters — an administrator may hold
several keys and that is what tells them apart. A Plex connector's secret is not a key
but the **account token** of the server's owner: there is only ever one, so those four
characters would identify nothing and would give away part of a credential that opens
the whole Plex account. It is masked entirely (`last4` is null).

**Stored Plex token (step 2).** The account-wide token of the owner, from an `owned`
account, encrypted like every secret. Step 2 needs it for the hourly user sync
(`GET https://plex.tv/api/users`). Whether to keep it or move to server-scoped tokens is
decided at step 3 (see the [roadmap](roadmap.md)).

**Who can change it.** After setup, `PUT /admin/connectors/media_server` needs:

- the effective role admin **and** `media_server_admin` true (otherwise `403`
  `media_server_admin_required`): a promoted admin cannot repoint the media server;
- a fresh re-authentication of the caller on the **currently configured** media
  server, less than 5 minutes old (otherwise `403` `reauth_required`; section 7).
  Credentials are never sent to the new URL before it is saved.

**Identity change.** If the saved server's identity (or kind) differs from the stored
one, the same transaction revokes every session (the caller's included), sets
`media_server_user_id` to null and `disabled_reason = unlinked` on every user, and stores
the new identity. Everyone signs in again; the new server's administrators become
admins as usual. Unlinked users keep their data but cannot sign in; relinking or
merging accounts is not part of v1.

**Unexpected identity.** When the identity read before a sign-in or by the sync does not
match the stored one (the server was reinstalled, or something else answers at that
address), Tindarr refuses sign-ins with `503` `media_server_changed` and logs an error.
It never re-links on its own. The admin fixes the URL from the console if a signed-in
media server administrator can still re-authenticate, otherwise the operator runs
`tindarr media-server reset` (section 3).

## 7. Sessions and tokens

### Lifetimes

| Session | Idle timeout | Absolute lifetime |
|---|---|---|
| `mobile` | 60 days without a refresh | 90 days after sign-in |
| `web` | 24 hours | 7 days |
| `setup` | none | 30 minutes |

`last_seen_at` is written at most once per minute per session. Refresh tokens expire at
the earlier of 60 days after issue and the session's absolute end.

### Access tokens (JWT)

- `HS256` only, with the HKDF sub-key `tindarr/v1/jwt-signing` of the master key
  (`KeyPurpose.JWT_SIGNING`). Library: PyJWT, with `algorithms=["HS256"]`.
- Header: `typ` = `at+jwt`, `kid` = the key id (`KeyMaterial.key_id`).
- Claims: `iss` = `tindarr:<install_id>`, `aud` = `tindarr-api`, `sub` (user id),
  `sid` (session id), `iat`, `exp` (15 minutes), `jti`. No role claim.
- Verification requires `typ`, `kid`, `iss`, `aud`, `sub`, `sid`, `iat` and `exp`, with at
  most 30 s of leeway. An expired token gives `401` `token_expired`; any other failure
  `401` `unauthorized`. Then the session and user are loaded (section 2).
- Changing the master key changes the signing key and the key id: everyone signs in
  again.

### Refresh tokens

- 256 random bits, base64url, stored as SHA-256 in `refresh_tokens`.
- **Rotation is an atomic compare-and-set** in one `BEGIN IMMEDIATE` transaction:
  `UPDATE refresh_tokens SET used_at = :now WHERE token_hash = :h AND used_at IS NULL
  AND expires_at > :now`. One row updated: insert the new token, return the pair. No row
  updated but the hash exists with `used_at` set: **reuse**, revoke the session
  (`401` `refresh_token_reused`). Unknown or expired: `401` `unauthorized`. Used hashes
  stay until their session is purged, so reuse is always detected.
- **Lost response.** If the refresh response never reaches the app (network cut after
  the server committed), the app still holds the old token; retrying it is a reuse and
  signs the user out. This is accepted: it is rare, and the fix (grace periods) would
  weaken theft detection ([ADR 0010](adr/0010-roles-and-refresh-tokens.md)). The app
  must not retry a refresh blindly on a timeout; it asks the user to sign in again.
- SQLite needs the explicit-transaction recipe (driver autocommit off, `BEGIN IMMEDIATE`
  emitted by the engine) for this, for pairing consumption and for migrations: see the
  storage notes in the [architecture](architecture.md#storage).

### Re-authentication (step-up)

`POST /auth/web/reauth` (web session + CSRF) takes a password, a completed Plex PIN
handle or a completed Quick Connect handle (purpose `reauth`). It is checked against
the currently configured media server; the account must be the session's user (same
`media_server_user_id`), otherwise `401` `invalid_credentials`. On success it refreshes
`media_server_admin`, `remote_access` and `name` like a sign-in and sets the session's
`reauth_at`. A web sign-in also sets `reauth_at`. Actions that need it (5 minutes):
changing the media server connector and changing `public_url`. Password re-auth
counts toward the password limits.

### Periodic sync

Every hour (first run 60 s after startup), with the admin API key or the Plex owner
token:

- Jellyfin / Emby: `GET /Users`. Plex: `GET https://plex.tv/api/users` (users whose
  shared servers include the stored `machineIdentifier`) plus the owner
  (`GET https://plex.tv/api/v2/user`).
- A linked user missing from the list, or disabled there (`Policy.IsDisabled`), is
  disabled with `disabled_reason = media_server` and their sessions are revoked.
- `media_server_admin` is **cleared** when the media server no longer says
  administrator; it is never set by the sync (only a sign-in sets it, so a demotion done
  in Tindarr lasts until that user's next sign-in, as ADR 0010 says).
- `remote_access` and `name` are updated.
- If the media server or plex.tv is unreachable, nothing changes and the sync retries
  at the next run. The identity is checked first (section 6).

### Admin role

Unchanged from [ADR 0010](adr/0010-roles-and-refresh-tokens.md): effective role `admin`
when `media_server_admin` or `promoted`; last-admin rule (`409` `last_admin`); only a
media server administrator can demote or disable another one (`403` `forbidden`) — or
sign them out (`DELETE /admin/users/{id}/sessions`), since doing that repeatedly would
come to the same thing. The last-admin rule counts `promoted` and `media_server_admin`
admins alike, so the only media server administrator can still delete their own data
while another admin remains; nobody can then change the connector or `public_url` until
they sign in again, which re-reads the flag from the media server.
On top of it, some actions are reserved to a media server administrator
(`403` `media_server_admin_required`), with a fresh re-authentication
(`403` `reauth_required`):

| Action | Admin | Media server admin | Fresh re-auth |
|---|---|---|---|
| Other connectors, settings, users, usage | yes | no | no |
| `PUT /admin/connectors/media_server` | yes | yes | yes |
| `PATCH /admin/settings` with `public_url` | yes | yes | yes |
| `password_sign_in` setting | yes | yes | no |

## 8. Rate limits

All limits are in memory (lost on restart, which needs host access anyway), keyed by
the resolved client IP (section 1) unless stated. Every refusal is `429` with
`code` = `rate_limited`, `retry_after_ms` in the body and `Retry-After` (seconds,
rounded up). **Global limits never refuse:** above a global threshold, each request is
answered after an extra 1 s delay, with at most 50 requests waiting; beyond that the
request gets `429` with `retry_after_ms` = 1000. An attacker can slow everyone down while
the traffic lasts, never lock anyone out.

**Bounded memory.** A limiter remembers at most 10 000 keys. When that table is full,
a limit keyed by the client address drops the least recently seen key: nobody is ever
refused because of somebody else's traffic. The per-username cap is the exception, and
deliberately so — it is all that stands between a guesser and the media server's own
lockout counter, so it never drops a bucket that still holds failures inside its
window. It prunes the expired ones, and if every remaining bucket is live it answers
`429` for the new name until one frees up. Flooding it with unknown names therefore
costs the attacker the flood and buys no extra attempt against a real account.

**Bounded queues.** A pause is a request, a socket and a task held open, so at most 50
of them sleep at the same time per limiter; a request arriving past that gets `429`
immediately (it is already past the threshold that earns a pause, so a refusal is
friendlier than the wait it replaces). The server itself serves at most 256 connections
at a time.

| What | Key | Limit | Over the limit |
|---|---|---|---|
| Failed password sign-ins and re-auths | client IP | 5 per 15 min | pause of 1 s, doubling at each further failure up to 15 min, at most 50 paused at once (`429` beyond) |
| Password failures forwarded to the media server | case-folded username | 2 per rolling 15 min | `429` until the oldest leaves the window; a success clears it. A live bucket is never evicted: a full table answers `429` for new names instead |
| Failed setup claims | client IP | 5 per 15 min | `429` |
| Failed setup claims | global | 20 per hour | slowdown |
| Pairing preview and pair | client IP | 10 per min | `429` |
| Pairing completion polls | client IP | 90 per min | `429` |
| Every pairing call | global | 300 per min | slowdown |
| Handle creation (Plex PIN, Quick Connect) | client IP | 10 per 15 min, 5 outstanding | `429` |
| Outstanding handles | global | 200 | the slot is taken from whichever client holds the most, so the table never grows; `429` only when the only handles left belong to the local network and the caller does not |
| Handle polling | handle | one upstream call per second | `202` with `retry_after_ms` |
| Token refresh | client IP | 30 per min | `429` |
| Other public endpoints (`server/info`) | client IP | 60 per min | `429` |
| Connection tests, `public_url` checks | session | 10 per min | `429` |
| Pending pairings | user | 3 | `429` |
| `public_url` checks in flight | global | 64 | `429` |

Completion has its own budget because the server tells the app to poll every 2 s for up
to 5 minutes: a shared limit of 10 per minute would refuse the app for doing exactly
what it was told, 16 seconds in. Preview and pair keep the tighter one — they are the
two calls somebody could grind against a code, and an app makes each of them once.

## 9. Phone pairing

1. **Create** (console, any signed-in user): `POST /pairings` needs `public_url`
   (`409` `public_url_not_set`). It returns the code (128 random bits, 22 base64url
   characters, stored as SHA-256, never shown again) and the link
   `tindarr://pair?server=<public_url>&code=<code>`. The console shows the QR code with
   the host of `public_url` written next to it ("connects to tindarr.example.com").
   Status `pending`, expires 5 minutes after creation whatever happens next.
2. **Preview** (app): `POST /auth/pair/preview` with the code returns only what the app
   must display: server name, user name, expiry. Anything other than a `pending`,
   unexpired code gets `410` `pairing_expired`, so the endpoint tells nothing to
   someone without a valid code.
3. **The app shows the host** of the `server` parameter prominently, in punycode for an
   internationalised name, with the server and user names, and asks for confirmation.
   It refuses `http` unless the host is an IP literal in a private range (section 1),
   `localhost` or a `.local` name, decided on the text of the URL with no DNS lookup.
   It says when an existing connection would be replaced.
4. **Request** (app): `POST /auth/pair` with the code, the device and a
   `code_challenge` (PKCE S256). The pairing becomes `awaiting_approval`, records the
   device, the client IP and the challenge, and the code stops working for preview and
   pair (`410`). The response carries a 4-digit **confirmation code** derived from the
   challenge (first 32 bits of SHA-256 of the challenge, modulo 10 000, zero-padded).
5. **Approve** (console): the console, polling `GET /pairings/{id}`, shows the device
   name, platform, IP and the confirmation code: "Approve only if your phone shows
   4821". The answer carries `retry_after_ms` (2 s while anything can still happen,
   null once the pairing is `completed`, `expired` or `revoked`), so the console stops
   polling on its own. `POST /pairings/{id}/approve` (CSRF) moves it to `approved`;
   `DELETE /pairings/{id}` rejects it (`revoked`).
6. **Complete** (app): the app polls `POST /auth/pair/complete` with the code and its
   `code_verifier` every 2 s: `202` while waiting, `403` `pairing_rejected` after a
   rejection, `410` once expired, and `200` with the token pair once approved. The
   `code_verifier` is checked **first**, so a caller who only holds the code learns
   nothing about the pairing's fate: everything but a matching verifier is the same
   `410`. That
   call opens the `mobile` session and marks the pairing `completed`. It applies the
   same checks as any authenticated request (user enabled, remote-access rule).
7. The console then shows "Pixel 9 connected" with a button that revokes that session.

Pairing does not contact the media server, so it does not re-sync the admin flag.

## 10. `public_url`

- An origin: `https://host[:port]`, or `http` only when the host is an IP literal in a
  private range, `localhost` or a `.local` name. No path, query, fragment or user info.
- Set during setup (after the first sign-in, the wizard proposes the origin the console
  is open on), later from the settings page, or by `TINDARR_PUBLIC_URL` (then locked).
- **Only a media server administrator with a fresh re-authentication** can change it
  (section 7).
- **Its host must already be an allowed host** (section 1), which the console's own
  origin is. An unknown host is refused before anything is called, with
  `409` `public_url_unverified` and `reason` = `host_not_allowed`, telling the operator
  to set `TINDARR_ALLOWED_HOSTS`. This is not a convenience: the proof below is bound
  to the host, so no answer from a host the server does not serve could pass.
- **Verified before saving.** The server creates a random nonce (kept in memory for
  60 s, answered once), requests `GET <public_url>/api/v1/server/info` with the header
  `Tindarr-Verify-Nonce: <nonce>`, without following redirects, with TLS verification
  and a 5 s deadline for the whole call, reading at most 64 KiB, and expects
  `public_url_proof` = base64url(HMAC-SHA256(key, `<nonce>|<host>`)) with the HKDF
  sub-key `tindarr/v1/public-url-proof`. `server/info` only answers `public_url_proof`
  when the nonce is one it is currently waiting for, and the proof it answers is for
  the `Host` **that request** arrived at. Failure: `409` `public_url_unverified`, with
  a coarse reason. A reverse proxy in front of this same instance passes; another
  server does not.
  - **Why the host is part of the proof.** Any address that can reach this server could
    otherwise relay: take the nonce out of the probe, ask this server for the proof over
    its own connection, and echo it back. To do that with a host-bound proof it would
    have to be served under the candidate host, and a host this server does not answer
    to is refused before routing. The residual case is a `public_url` whose host is an
    IP literal, which is always an allowed host: an administrator who deliberately
    types an attacker's IP address there is choosing it.
- Servers that cannot reach their own public address (no NAT hairpinning) cannot pass
  the check from the console; the operator sets `TINDARR_PUBLIC_URL` instead. An
  environment value is checked once after startup and only logged when it fails.
- Its host becomes an allowed host (section 1), and is already accepted while its own
  check runs, so an address can be set from a console opened elsewhere. It is not an
  extra accepted `Origin`.
- Clearing it (`null`) needs the same administrator and the same fresh
  re-authentication, and is not checked: there is nothing to check.

## 11. `auth_methods` in `server/info`

- `[]` while `setup_required` is true, whatever is configured. During setup, the
  wizard reads the methods from `GET /setup/state`.
- Jellyfin: `password` (unless `password_sign_in` is `disabled`, or `lan_only` and the
  caller's IP is not private) and `quick_connect` when the cached
  `GET /QuickConnect/Enabled` says so (left out while unknown).
- Emby: `password`, same rule. Plex: `plex_pin`.
- `pairing` once `public_url` is set.

## 12. Tables

Added by migration `0002` in step 2. Ids are 128-bit random values, base64url, generated
by the server. Times are UTC (`UtcDateTime`).

**`server_state`** (existing single row), new columns:

| Column | Type | Notes |
|---|---|---|
| `install_id` | text, not null | Random, generated by the migration. `DeviceId` for Jellyfin/Emby, JWT `iss`. |
| `setup_code_hash` | text, null | SHA-256 of the setup code while setup is pending. |
| `media_server_identity` | text, null | `jellyfin:<id>`, `emby:<id>` or `plex:<machineIdentifier>`. |

**`users`**

| Column | Type | Notes |
|---|---|---|
| `id` | text, PK | |
| `media_server_user_id` | text, unique, null | Normalised (section 4). Null when unlinked. |
| `name` | text, not null | From the media server. |
| `media_server_admin` | boolean, not null | ADR 0010. |
| `promoted` | boolean, not null, default false | ADR 0010. |
| `remote_access` | boolean, not null, default true | From the media server policy. |
| `enabled` | boolean, not null, default true | |
| `disabled_reason` | text, null | `admin`, `media_server` or `unlinked`; null when enabled. |
| `daily_generation_limit` | integer, null | Null = server default (used from step 4). |
| `created_at`, `last_sign_in_at`, `last_seen_at`, `synced_at` | datetime | Last three nullable. |

**`sessions`**

| Column | Type | Notes |
|---|---|---|
| `id` | text, PK | JWT `sid`, `Session.id`. |
| `kind` | text, not null | `mobile`, `web` or `setup` (check constraint). |
| `user_id` | text, FK `users` (cascade), null | Null only for `setup` (check constraint). Indexed. |
| `token_hash` | text, unique, null | SHA-256 of the cookie token (`web`, `setup`); null for `mobile`. |
| `csrf_token` | text, null | `web` and `setup`; returned by `GET /auth/web/session`. |
| `device_name`, `platform`, `app_version` | text, null | From `DeviceInput`, or derived from the User-Agent for `web` (`platform` = `web`). Null only for a row written without either; `Session` in the contract shows them as nullable for that reason. |
| `created_at`, `last_seen_at`, `expires_at` | datetime, not null | `expires_at` = absolute end. |
| `reauth_at` | datetime, null | `web` only. |
| `revoked_at` | datetime, null | |
| `revoked_reason` | text, null | `logout`, `user`, `admin`, `disabled`, `reuse`, `setup_completed`, `superseded`, `server_changed`. |

**`refresh_tokens`**

| Column | Type | Notes |
|---|---|---|
| `token_hash` | text, PK | SHA-256. |
| `session_id` | text, FK `sessions` (cascade), not null | Indexed. |
| `created_at`, `expires_at` | datetime, not null | |
| `used_at` | datetime, null | Set by rotation; kept for reuse detection. |

**`pairings`**

| Column | Type | Notes |
|---|---|---|
| `id` | text, PK | |
| `user_id` | text, FK `users` (cascade), not null | Indexed. |
| `code_hash` | text, unique, not null | SHA-256 of the code. |
| `status` | text, not null | `pending`, `awaiting_approval`, `approved`, `completed`, `revoked` (expiry is computed from `expires_at`). |
| `code_challenge` | text, null | From `POST /auth/pair`. |
| `device_name`, `platform`, `app_version`, `requested_from` | text, null | Set by `POST /auth/pair`; `requested_from` is the client IP. |
| `created_at`, `expires_at` | datetime, not null | |
| `requested_at`, `approved_at`, `completed_at`, `revoked_at` | datetime, null | |
| `session_id` | text, FK `sessions` (set null), null | The `mobile` session it opened. |

**Purge** (daily job): sessions revoked or expired for more than 30 days, with their
refresh tokens; pairings older than 7 days.

**Also stored:** `media_server_name`, what the media server calls itself,
written at each successful connection test so the wizard and `GET /server/info` can show
"connected to Home Jellyfin" without testing again. It is never set by hand.

**In memory, not in the database:** handles (section 5), rate-limit counters
(section 8), `public_url` nonces (section 10), the Quick Connect "enabled" cache and the
last identity check (section 6). All are lost on restart, with the consequences given
in each section.

## 13. Logging rules for this step

- Redacted by key name (in addition to the existing list): `setup_code`, `code`,
  `pairing_code`, `code_verifier`, `code_challenge`, `pin_id`, `handle`, `secret`,
  `refresh_token`, `access_token`, `session`, `sid`, `jwt`, `csrf`, `otp`, `pin`,
  `nonce`.
- Credentials never go in a URL path or query string Tindarr serves: codes, handles
  and tokens travel in bodies, cookies or headers. Resource identifiers are not
  credentials and may appear in a path (`/me/sessions/{session_id}`,
  `/pairings/{pairing_id}`, `/admin/users/{user_id}`): they name a row the caller must
  already be authorised for, and knowing one grants nothing. Outbound calls that need one in a
  query (Quick Connect's `secret`) go through an HTTP client whose logger stays at
  `WARNING`, and URLs are redacted before any log line.
- Validation errors never echo the input (already the case since step 1).
- Security events are logged at `INFO` without secrets: sign-in success and failure
  (user id or case-folded username hash, method, client IP), refresh-token reuse,
  session revocations, pairing approvals, connector and `public_url` changes, identity
  mismatches, rate-limit trips.
