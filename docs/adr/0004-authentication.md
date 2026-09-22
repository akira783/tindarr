# 0004. Authentication delegated to the media server, first-run claim

- Status: accepted
- Date: 2026-09-22
- Amended by: [0009](0009-web-console-and-phone-pairing.md),
  [0010](0010-roles-and-refresh-tokens.md),
  [0011](0011-hardening-after-the-pre-step-2-review.md)

> **Note (2026-09-22):** superseded in part.
> - First run happens in the web console, and the setup token became a setup session
>   cookie ([ADR 0009](0009-web-console-and-phone-pairing.md)).
> - Jellyfin Quick Connect and phone pairing by QR code are added as ways to sign in,
>   and the console uses cookie sessions (ADR 0009).
> - The admin flag is re-synced at every sign-in, a Tindeerr role is
>   `media_server_admin` or `promoted`, and access tokens drop the `role` claim;
>   refresh-token reuse has no grace period and the app must refresh single-flight
>   ([ADR 0010](0010-roles-and-refresh-tokens.md)).
> - **2026-09-22, [ADR 0011](0011-hardening-after-the-pre-step-2-review.md):** one
>   active setup session, completion bound to the claiming browser, only the code's
>   path is logged. Jellyfin/Emby calls use the `Authorization: MediaBrowser … Token=`
>   header; users whose remote access is disabled are refused from outside the LAN;
>   password failures forwarded to the media server are capped per username. Plex
>   sign-in matches the stored `machineIdentifier`, users are keyed by plex.tv account
>   id, and the sign-in's plex.tv device is deleted after the check, instead of the
>   token being merely discarded. Every request re-checks its session and user;
>   sessions get absolute lifetimes; an hourly sync with the media server disables
>   removed users. Details in [the authentication reference](../auth.md).
>
> The text below is kept as decided.

## Context

Users already have an account on their media server, and Swipe needs to know which
media server user someone is to read their history. Public deployments may be
reachable from the internet before they are configured.

## Decision

**First run (claim).**
1. On first start, the server generates a one-time setup code (random, at least
   60 bits), logs it, and writes it to `data/setup-code` (0600).
2. The app detects `setup_required` in `server/info` and asks for the code.
   `POST /setup/claim` exchanges the code for a short-lived setup token. A setup token
   is a different token type from an access token: each is rejected where the other is
   expected.
3. With that token, the admin configures the media server
   (`PUT /setup/media-server`). The server tests the connection before saving.
4. The first media server **administrator** who signs in completes setup and becomes a
   Tindeerr admin. The setup code is then deleted.

**Sign-in.**
- **Jellyfin / Emby.** The app sends username and password to Tindeerr, which checks
  them with `POST /Users/AuthenticateByName` and reads the administrator flag from the
  returned user policy. It then discards the password and ends the media server
  session it just opened (`POST /Sessions/Logout` with the returned token), so no
  stray device is left in the user's media server account.
- **Plex.** A PIN flow brokered by the server: the server creates a strong plex.tv PIN
  (`POST https://plex.tv/api/v2/pins`) with its own client identifier, and gives the
  app the `app.plex.tv/auth` link and a random, single-use handle for it (never the
  plex.tv PIN id). Each time the app polls with that handle, the server checks the PIN
  on plex.tv. Once it carries a token, the server uses it once to check that the Plex
  account can access the configured server (`owned` there means administrator), then
  discards it. The only Plex token ever stored is the owner token entered during
  setup or by an admin.
- Only users of the configured media server can sign in. Admins can disable a user.

**Tokens.**
- **Access tokens.** JWTs, valid 15 min. Claims: `sub` (user id), `sid` (session id),
  `role`. They are signed with a key derived from the server secret.
- **Refresh tokens.** Opaque, 256 bits, stored hashed and rotated on every use.
  Presenting an already-used refresh token revokes the session. They expire after
  60 days idle.
- **Sessions.** One per device (name, platform, last seen), listable and revocable.

## Consequences

- No passwords to store, reset or leak, and no separate Tindeerr accounts.
- Roles follow the media server: admins there are admins here, and admins can promote
  others.
- An attacker who finds a fresh install still needs the setup code, which only
  someone with access to the server's logs or files has.
- Adding OIDC (Authentik, Authelia…) later is one more sign-in method issuing the same
  tokens.

## Alternatives considered

- **Local Tindeerr accounts.** Another password to manage, and a manual mapping to
  media server users. Rejected.
- **Proxy forward-auth (Authentik…) in front of the API.** Mobile apps cannot follow
  browser redirects, and many users have no identity provider. Could come later as an
  OIDC sign-in method.
- **"First user to open the app is admin".** This is how an internet-exposed fresh
  install gets hijacked. Rejected.
