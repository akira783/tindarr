# 0004. Authentication delegated to the media server, first-run claim

- Status: accepted
- Date: 2026-09-22

## Context

Users already have an account on their media server, and Swipe needs to know which
media server user someone is to read their history. Public deployments may be
reachable from the internet before they are configured.

## Decision

**First run (claim).**
1. On first start, the server generates a one-time setup code, logs it, and writes it
   to `data/setup-code` (0600).
2. The app detects `setup_required` in `server/info` and asks for the code.
   `POST /setup/claim` exchanges the code for a short-lived setup token.
3. With that token, the admin configures the media server
   (`PUT /setup/media-server`). The server tests the connection before saving.
4. The first media server **administrator** who signs in completes setup and becomes a
   Tindeerr admin. The setup code is then deleted.

**Sign-in.**
- **Jellyfin / Emby.** The app sends username and password to Tindeerr, which checks
  them with `Users/AuthenticateByName` and then discards the password and the
  returned user token.
- **Plex.** A PIN flow brokered by the server: the server creates a plex.tv PIN, the
  app opens the plex.tv link, and the server polls the PIN. The server keeps no user
  token. It checks that the Plex account has access to the configured server.
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
