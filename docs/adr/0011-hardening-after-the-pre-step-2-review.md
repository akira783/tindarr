# 0011. Hardening after the pre-step-2 review

- Status: accepted
- Date: 2026-09-22
- Amends: [0004](0004-authentication.md), [0005](0005-adapters-and-ai-providers.md),
  [0006](0006-api-contract-and-versioning.md),
  [0009](0009-web-console-and-phone-pairing.md),
  [0010](0010-roles-and-refresh-tokens.md)

## Context

Before step 2, four reviews read the design: an adversarial review of the
authentication design, a consistency and readiness review, a review of the step-1
code, and a check of every third-party API fact against upstream sources. They found
real attacks the design allowed, facts that were wrong (Jellyfin 12 no longer accepts
`X-Emby-Token`), and gaps that would have forced whoever implements step 2 to guess.

The main attacks found:

- Behind a reverse proxy, the TCP peer is always a private address. Without a defined
  trusted-proxy rule, the plain-HTTP console opt-in applied to the whole Internet,
  every per-IP limit shared one bucket, and anyone could spoof `X-Forwarded-For`.
- A promoted admin, or anyone holding an admin cookie, could point the media server
  connector at a fake Jellyfin and collect every user's password at their next sign-in,
  or at their own server to become a media server administrator.
- plex.tv resources are self-reported: matching the configured Plex server by name or
  URL lets another server pass as it, and any user who can see the PIN link could
  supply the "owner" token during setup.
- Tindeerr forwards password attempts to the media server, so it exposed a LAN-only
  Jellyfin's account lockout to the Internet.
- A leaked setup code (logs shipped to Loki or Portainer) let a stranger swap the media
  server URL just before the owner signed in; pairing codes could be sent to an
  attacker's host through `public_url`; a disabled user kept a working access token for
  15 minutes and a deleted media server user kept their sessions forever.
- DNS rebinding: the server took its own origin from the `Host` header.

The owner's standing rule for judgment calls is "choose the most secure option".

## Decision

The exact rules and numbers are in [the authentication reference](../auth.md); this
records the decisions and why.

**Network context.**
- The client IP is the rightmost untrusted `X-Forwarded-For` hop, honoured only from
  `TINDEERR_TRUSTED_PROXIES`; the scheme comes from `X-Forwarded-Proto` only from a
  trusted proxy; `X-Forwarded-Host` is never used.
- "Private network" is decided on the resolved client IP, by IP ranges only, with no
  DNS lookup. Carrier-grade NAT (`100.64.0.0/10`, Tailscale) is not private.
- `Host` must be an IP literal, `localhost`, a name in `TINDEERR_ALLOWED_HOSTS` or the
  host of `public_url`; the expected `Origin` is built from that validated host.
  `public_url` is no longer a second accepted origin.

**Media server.**
- Jellyfin and Emby are called with
  `Authorization: MediaBrowser Client, Device, DeviceId, Version, Token`, with a
  `DeviceId` stable per install and a constant `Version`. Minimum Jellyfin 10.10,
  detected through `/System/Info/Public`.
- The configured server's identity (Jellyfin/Emby server id, Plex
  `machineIdentifier` read from the server's own `/identity`) is stored. Only a media
  server administrator, after re-authenticating on the **current** server, can change
  the connector. A change of identity revokes every session and unlinks every user. An
  unexpected identity stops sign-ins until an admin or the operator (CLI reset) acts.
- Users whose media server policy forbids remote access are refused when their client
  IP is not private, at sign-in and on every request, since the media server only sees
  Tindeerr's LAN address.
- Password sign-in forwards at most 2 failures per case-folded username per 15 minutes
  to the media server, below Jellyfin's default lockout of 3. It pauses instead of
  locking. The `password_sign_in` setting (`enabled`, `lan_only`, `disabled`) lets an
  admin remove the password path from the Internet; Quick Connect, Plex PIN and pairing
  remain.
- An hourly sync with the media server disables deleted or disabled users and clears
  the admin flag of users who lost it there (it never sets it).

**Plex.**
- Sign-in is accepted only if a plex.tv resource matches the stored
  `machineIdentifier`; administrator only if `owned` on that resource; users are keyed
  by plex.tv account id. The owner token must come from an `owned` account.
- Each sign-in PIN uses its own client identifier, and the resulting plex.tv device is
  deleted after the check (unofficial endpoint, best effort, logged when it fails).
- Tindeerr never registers a JWK with plex.tv. Managed Plex Home profiles cannot sign
  in (documented limitation).
- Step 2 stores the owner's account-wide token, which the user sync needs. Whether to
  keep it or use server-scoped and per-user tokens is decided at step 3, together with
  per-user watch progress.

**Handles** (Plex PINs, Quick Connect) are bound to a purpose (`sign_in`, `reauth`,
`owner_token`) and to their initiator: a pre-auth cookie for the console, a PKCE S256
verifier for the app, the session for the others. They live in memory, with a cap per
IP and a global cap. Setup gets a read-only PIN status endpoint instead of consuming the
PIN. Abandoned Quick Connect approvals are cleaned up.

**Setup.** One active setup session (a new claim revokes the previous one), in its own
cookie `__Host-tindeerr_setup`. Completion requires that cookie, revokes the setup
session and issues a new web session. Logs give the path of the setup code, never the
code. A media server locked by the environment skips the wizard step
(`setting_locked`). `tindeerr media-server reset` is the host-side recovery path.

**`public_url`** is verified before saving (HMAC proof of a nonce through
`server/info` on that URL), can only be changed by a media server administrator with a
fresh re-authentication, and the console shows its host next to the QR code.

**Sessions and tokens.**
- Every authenticated request loads its session and its user: revocation and disabling
  are immediate.
- Absolute lifetimes: app 90 days, web 7 days, setup 30 minutes.
- JWT: HS256 with its HKDF sub-key, required `typ`/`kid`/`iss`/`aud`, 30 s leeway, no
  role claim.
- Refresh rotation is an atomic compare-and-set. A lost refresh response signs the user
  out; that is accepted and documented.
- A step-up re-authentication (`POST /auth/web/reauth`, 5 minutes) guards the media
  server connector and `public_url`.

**Pairing** stays QR-only and gains a **console approval**: after the app asks to pair
(with a PKCE challenge), the console shows the device, its IP and a 4-digit
confirmation code that the phone also shows; tokens are issued only after the user
clicks Approve, while the app polls. The app shows the target host prominently
(punycode) and refuses `http` for non-private hosts.

**Rate limits** are in memory, per resolved client IP. Global thresholds only slow
requests down; the only global refusal is the cap on outstanding handles, a memory
bound.

**Smaller rules.**
- `DELETE /me` and revoking another session are console-only.
- A bearer token on a console endpoint gets `401 unauthorized`; a non-admin gets
  `403 admin_required`.
- Security headers depend on the path (API, console page, hashed assets).
- The SPA fallback never applies under `/api`.
- The auth layer is pure functions; cookies and headers are handled in the API layer.
- The image is built from the repository root with a Node stage.
- The console's generated API client is committed and checked in CI.

**Request backend.** Target Seerr v3.x. Jellyseerr is accepted as a legacy
deployment, Overseerr for Plex only (it has no Jellyfin user ids). Backend users are
matched by listing them with the admin key **without** `X-API-User` (paged), Jellyfin
ids normalised. Requests keep `X-API-User`, which makes Seerr run the whole request as
that user, auto-approval, overrides and quotas included (checked in Seerr's source).

## Consequences

- Step 2 grows: trusted-proxy handling, an allowed-hosts check, a user sync job,
  step-up re-authentication, pairing approval, the Plex device cleanup, a CLI reset and
  a larger test suite. In return, none of the attacks above works against the design
  as written.
- Operators behind a reverse proxy must set `TINDEERR_TRUSTED_PROXIES`, and those using
  a domain name `TINDEERR_PUBLIC_URL` or `TINDEERR_ALLOWED_HOSTS`. Misconfiguration
  fails closed (wrong client IP, `host_not_allowed`), and the server logs what to fix.
- An attacker can pause password sign-in for a known username (15 minutes at a time),
  and Jellyfin's cumulative counter can still lock an account over several windows.
  Internet-exposed servers should use `password_sign_in = lan_only`.
- Pairing needs one more click in the console, and a lost refresh response signs the
  user out.
- Servers that cannot reach their own public address set `public_url` through the
  environment.
- Changing the media server to a different one signs everyone out and starts users
  from empty accounts; relinking is not in v1.

## Alternatives considered

- **Hard per-username lockout in Tindeerr.** Lets anyone lock out the admin. Rejected
  for a pause plus the `password_sign_in` setting.
- **Global hard limits on claim and pairing.** With 60- and 128-bit codes they only help
  an attacker block legitimate use. Rejected for slowdowns.
- **Forwarding `X-Forwarded-For` to Jellyfin** so it enforces remote access itself.
  Only works if Tindeerr is a trusted proxy in Jellyfin's own settings, which most
  setups will not configure. Rejected in favour of Tindeerr checking the policy.
- **Re-authenticating against the new media server URL** when changing it. The new
  server's identity is self-reported and can be cloned, so a password could be sent to
  a fake server. Rejected: re-authentication always targets the current server.
- **Showing a short confirmation code instead of approving in the console.** The code
  alone does not stop someone who photographed the QR code from pairing first.
  Kept as an addition to the approval, not a replacement.
