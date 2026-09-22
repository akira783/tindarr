# 0009. Web console for setup and admin, phones paired by QR code

- Status: accepted
- Date: 2026-09-22
- Amends: [0001](0001-self-hosted-server-and-mobile-app.md),
  [0003](0003-mobile-stack.md), [0004](0004-authentication.md),
  [0006](0006-api-contract-and-versioning.md)

## Context

ADRs 0001, 0003 and 0004 put every task in the mobile app, admin work included:
first-run claim, connectors, AI provider and model, server settings, users, usage.
These are forms with long URLs and API keys, done once or rarely, on a computer next
to the server. Typing them on a phone is slow and error-prone, and the admin screens
would make up a large part of the app for a small part of its users.

Signing in on a phone has its own problems: some Jellyfin accounts have no usable
password (SSO through a plugin), and typing a server URL plus credentials on a phone
is the first thing a new user has to get right.

## Decision

**A web console served by the server.**

- A small single-page app in `web/`: React, Vite and TypeScript `strict`. Its API
  client is generated from `api/openapi.yaml` with the same tools as the app
  (`openapi-typescript` + `openapi-fetch`), and it uses the same translation catalogs
  (`shared/i18n/`, en and fr).
- It is built in the server image's build stage and served by the server as static
  files: the console under `/`, the API under `/api`, the probe at `/healthz`. Any
  other `GET` that is not a file returns `index.html` (client-side routing). Hashed
  assets are cached as immutable; `index.html` is not cached. Node is not part of the
  runtime image.
- **The console does all admin work:** first-run setup (claim with the setup code,
  media server), connectors, AI provider and model, server settings (including
  `public_url`), users, AI usage. Every signed-in user, admin or not, can also open it
  to pair a phone and manage their own sessions.
- **The mobile app keeps** the deck, likes, taste profile, preferences and sessions.
  Its admin screens are dropped. It may show a read-only server status later.
- **First run.** The setup code is entered in the console. An app pointed at a server
  whose `server/info` says `setup_required` only tells the user to open the console
  at the same URL.

**Sign-in on the console.** The same methods as the app: Jellyfin / Emby password,
Plex PIN, plus **Jellyfin Quick Connect**.

- Quick Connect: the server calls `POST /QuickConnect/Initiate` (with its own client
  identification header), shows the user the returned code and keeps the returned
  secret to itself. The user approves the code in any Jellyfin client where they are
  signed in. The server polls `GET /QuickConnect/Connect?secret=…` on each poll from
  the client, and once it reports `Authenticated`, calls
  `POST /Users/AuthenticateWithQuickConnect` with the secret. From there it is the
  password flow: read the administrator flag, end the Jellyfin session it opened,
  keep nothing. `GET /QuickConnect/Enabled` tells whether the Jellyfin admin turned
  the feature on.
- As with the Plex PIN, the client polls with a random single-use handle (at least
  128 bits), never the Jellyfin secret.
- Quick Connect is offered to the app too: it is the same flow ending in a token pair
  instead of a cookie. Emby has no Quick Connect.

**Console sessions use a cookie, the app keeps bearer tokens.**

- The cookie is `__Host-tindeerr_session`: `HttpOnly`, `Secure`, `SameSite=Strict`,
  `Path=/`, no `Domain`. Its value is an opaque 256-bit token, stored hashed in the
  `sessions` table with `kind` = `web`. No token the console holds can be read by
  JavaScript. A web session expires after 24 h without activity and 7 days after
  sign-in, whichever comes first. The cookie is only ever set by the web sign-in
  endpoints (`/api/v1/auth/web/…`) and the setup claim.
- **CSRF: a synchronizer token bound to the session, plus an Origin check.** Each web
  session has its own random CSRF token (at least 128 bits), stored with the session
  row and returned in the body of the sign-in response and of
  `GET /api/v1/auth/web/session` (so the console gets it back after a reload). It is
  kept in memory by the console and sent as `X-CSRF-Token` on every `POST`, `PUT`,
  `PATCH` and `DELETE`, where the server compares it in constant time. On the same
  requests, the `Origin` header must be the server's own origin (as seen after the
  trusted proxy headers) or `public_url`; a missing `Origin` is refused. The web
  sign-in endpoints and the setup claim, which have no session yet, check `Origin`
  too (login CSRF).
  - Why not a double-submit cookie: its security rests on the attacker being unable
    to write a cookie for the origin, and it needs a cookie readable by JavaScript.
    A token bound to the server-side session has neither weakness.
  - Why not a custom header alone: it only works because CORS preflight blocks
    cross-origin custom headers, one browser behaviour away from failing. The token
    costs one column and one comparison.
  - `SameSite=Strict` already stops the browser from sending the cookie on
    cross-site requests; the token and the Origin check are the layers that do not
    depend on it.
- **How the API authenticates a request.** An `Authorization: Bearer` header is
  checked as an access token, and the cookie is then ignored (a browser never adds
  that header on its own, so no CSRF check applies). Otherwise the session cookie is
  checked, with the CSRF token and `Origin` on unsafe methods. Endpoints shared by
  both clients (`/me`, `/swipe/…`, `/auth/logout`) accept either. **Console-only
  endpoints** (`/admin/…`, `/pairings/…`) accept only a web session: a token stolen
  from a phone cannot change connectors or create pairings. Setup endpoints accept
  only the setup session given by the claim (same cookie, `kind` = `setup`, 30
  minutes, no user, its own CSRF token). It replaces the setup bearer token of
  ADR 0004.
- **CORS stays disabled.** The console is same-origin; the Vite dev server proxies
  `/api` to a local server, so development is same-origin too.
- **HTTPS.** `Secure` cookies need HTTPS, except on `localhost`, which browsers treat
  as a secure context. Over plain HTTP from another host, the console refuses to sign
  in (`code` = `https_required`) and explains how to put it behind TLS or reach it
  through an SSH tunnel. An operator can opt in to plain HTTP on a private network
  with `TINDEERR_ALLOW_HTTP_CONSOLE=true`: the cookie then drops `Secure` and the
  `__Host-` prefix (it is named `tindeerr_session`), only for requests from private
  and link-local addresses, and the server logs a warning at startup.

**Content-Security-Policy for the console:**

```
default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'
https://image.tmdb.org; connect-src 'self'; font-src 'self'; object-src 'none';
base-uri 'none'; form-action 'self'; frame-ancestors 'none';
require-trusted-types-for 'script'
```

No inline script, no `eval`, no third-party script. The console shows no trailers, so
YouTube is not allowed. Images from the media server (user avatars) are not loaded
directly; if the console shows them, the server proxies them under `/api`. API responses keep their own `default-src 'none'` policy.
`Cross-Origin-Opener-Policy: same-origin` is added to console pages.

**Phone pairing by QR code.**

1. A signed-in user clicks "Connect a phone" in the console. The server creates a
   pairing bound to that user: a random code of at least 128 bits (22 base64url
   characters), stored as a SHA-256 hash, single-use, valid 5 minutes. A user has at
   most 3 pending pairings.
2. The console shows a QR code for
   `tindeerr://pair?server=<public_url>&code=<code>`. On a phone browser, it shows an
   "Open in Tindeerr" button with the same link instead.
3. The app scans the QR code with its own scanner (a link opened from the system
   camera works too). It calls `POST /api/v1/auth/pair/preview`, which returns the
   server name and the user name without consuming the code. It shows the server URL
   and "You will be signed in as <name>", and asks for confirmation. When the app is
   already connected to another server, it says that connection will be replaced.
   The app refuses a `server` that uses `http` unless the host is private,
   link-local or `.local`.
4. On confirmation, the app calls `POST /api/v1/auth/pair` with the code and its
   device info, and gets the same token pair as any sign-in. The pairing is consumed
   and a `mobile` session is created.
5. The console polls the pairing (`GET /api/v1/pairings/{id}`) and shows "Pixel 9
   connected", with a button that revokes that session.

- **QR only, no short typed code.** A code short enough to type (6 to 8 characters)
  has 30 to 40 bits: it would need tight rate limits and a shorter life, and it would
  add a guessable credential to the public surface. Someone who cannot scan signs in
  the normal way (password, Plex PIN, Quick Connect).
- Pairing does not contact the media server, so it does not re-sync the admin flag
  ([ADR 0010](0010-roles-and-refresh-tokens.md)); the web sign-in that preceded it
  did.

**Public URL.** A server setting `public_url` (admin-set, or
`TINDEERR_PUBLIC_URL`), used only to build the QR link. It is an origin
(scheme, host, optional port, no path): `https`, or `http` only for a private,
link-local, `.local` or `localhost` host. The setup wizard proposes the origin the
console is open on. Without it, the console cannot create pairings
(`code` = `public_url_not_set`), and `pairing` is left out of `auth_methods`.

## Consequences

- The app shrinks to what is used daily. Admin forms get a keyboard and a large
  screen.
- One more front-end to maintain, but it shares the contract, the generated client
  and the translations with the app, and it ships inside the server image: no extra
  deployment.
- The server now serves browser pages, so it needs browser defences: cookie flags,
  CSRF, CSP, clickjacking protection. These are specified here and tested at step 2.
- Two credential types reach the API (bearer and cookie). The rule for which one an
  endpoint takes is written in the contract (`security` of each operation).
- Signing in a new phone becomes one scan from a computer where the user is already
  signed in.
- A server behind plain HTTP on a LAN needs an explicit opt-in for the console.

## Alternatives considered

- **Keep admin screens in the app.** Long forms on a phone, and a larger app for
  every user. Rejected.
- **Console session with bearer tokens in browser storage.** Any XSS would read them.
  Rejected in favour of an `HttpOnly` cookie.
- **Server-rendered pages (Jinja) instead of an SPA.** No build step, but no reuse of
  the generated client and translations, and a second way of writing UI. Rejected.
- **Console as a separate container or origin.** Needs CORS or a proxy, and one more
  thing to deploy. Rejected.
- **Short typed pairing code as a fallback.** See above. Rejected.
- **Pairing by showing the phone's code in the console** (reverse direction). The
  phone would have to reach an unauthenticated server endpoint to register a code
  first, which opens the same guessing problem. Rejected.
