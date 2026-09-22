# 0010. Admin role synced from the media server, strict refresh-token reuse

- Status: accepted
- Date: 2026-09-22
- Amends: [0004](0004-authentication.md)
- Amended by: [0011](0011-hardening-after-the-pre-step-2-review.md)

> **Note (2026-09-22, [ADR 0011](0011-hardening-after-the-pre-step-2-review.md)):**
> - An hourly sync with the media server also **clears** `media_server_admin` when the
>   flag was removed there (it never sets it, so a demotion done in Tindeerr still lasts
>   until that user's next sign-in), and disables users removed or disabled there.
> - Changing the media server connector or `public_url` needs a media server
>   administrator with a fresh re-authentication, not just an admin.
> - Refresh rotation is an atomic compare-and-set; a lost refresh response signs the
>   user out (accepted). Sessions have absolute lifetimes (app 90 days, web 7 days).

## Context

ADR 0004 says media server administrators become Tindeerr admins and that admins
can promote or demote others, without saying what happens when the two disagree:
someone removed as administrator on the media server, or demoted in Tindeerr while
still administrator there. It also says a reused refresh token revokes the session,
without saying whether a short grace period covers two concurrent refreshes.

## Decision

**Admin role.**

- Each user row stores two flags:
  - `media_server_admin`: written from the media server at **every sign-in**
    (password, Plex PIN, Quick Connect). Token refresh and phone pairing do not
    contact the media server and leave it as it is.
  - `promoted`: set by a Tindeerr admin. It is the only role field an admin edits.
- **Effective role:** `admin` if `media_server_admin` or `promoted`, else `user`.
- Consequences of the rule:
  - Someone removed as administrator on the media server loses Tindeerr admin at
    their next sign-in, unless an admin promoted them in Tindeerr.
  - Promoting a non-admin sets `promoted`; it survives sign-ins.
  - Demoting a user clears `promoted`, and clears `media_server_admin` until that
    user's next sign-in. **A demotion done only in Tindeerr is overridden at the
    next sign-in of a media server administrator.** The console says so before
    demoting one. To remove them for good, remove the flag on the media server.
  - Only an admin who is a media server administrator can demote or disable
    another media server administrator. A promoted admin cannot lock out the media
    server's own administrators.
- **Last admin.** An admin cannot demote or disable the last enabled admin, or
  delete their own data while they are that admin (`code` = `last_admin`). A sign-in
  can still leave zero admins, when the only one loses the flag on the media server.
  That is recoverable without Tindeerr: the next media server administrator who signs
  in is an admin again, and every media server has one.
- **Authorization reads the database.** The access token no longer carries a `role`
  claim. Admin endpoints are console-only ([ADR 0009](0009-web-console-and-phone-pairing.md))
  and check the effective role and the `enabled` flag of the session's user on every
  request, so a demotion or a disable takes effect at once.

**Refresh tokens: strict reuse detection, no grace period.**

- A refresh token works once. Presenting a used one, even a second after it was
  rotated, revokes the whole session (`code` = `refresh_token_reused`); the user signs
  in again.
- **Requirement for the app: single-flight refresh.** At most one refresh request is
  in flight. Requests that get `token_expired` while it runs wait for it and retry
  with the new access token. The new pair is written to secure storage before any
  waiting request is released. This is covered by unit tests in the app (concurrent
  requests with an expired token cause exactly one refresh) at step 6.

## Consequences

- The media server stays the source of truth for who administers it, with
  Tindeerr promotions on top.
- A reused refresh token is a clear signal of theft or of a client bug, never a race
  the server has to guess about. The price is that a client that refreshes twice in
  parallel signs its user out, hence the tested single-flight requirement.

## Alternatives considered

- **Role set once at first sign-in, then managed only in Tindeerr.** A media server
  administrator who is removed there would stay admin here indefinitely. Rejected.
- **Role taken only from the media server, no promotion.** Some households want a
  second admin who is not a media server administrator. Rejected.
- **Grace period (a used refresh token accepted again for a few seconds).** Hides
  client races, but gives a thief who replays quickly a window, and makes reuse
  detection depend on timing. Rejected.
