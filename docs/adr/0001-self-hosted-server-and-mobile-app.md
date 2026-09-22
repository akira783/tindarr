# 0001. A self-hosted server plus a mobile app

- Status: accepted
- Date: 2026-09-22
- Amended by: [0009](0009-web-console-and-phone-pairing.md)

> **Note (2026-09-22):** the server also serves a web console, which does setup and
> all admin work; the app keeps the daily features. See ADR 0009. The decision below
> (one server per household, no central service) is unchanged.

## Context

Swipe needs API keys (AI provider, TMDb, request backend, media server admin), runs
paid AI calls in the background, and reads watch history that lives on the user's own
media server. The app is meant for the public, so for many households.

## Decision

Each household runs its own Tindarr server, next to its media server, typically as a
Docker container. The mobile app is a client of that server: the user types the server
URL and signs in. There is no central Tindarr service.

## Consequences

- Keys never reach phones. Background work (warm-up, profile rewrite) does not depend
  on the phone being awake.
- Several users of one household share one configuration, each with their own votes and
  profile.
- Users must deploy a server. This matches the audience: people who already run
  Jellyfin / Plex and Seerr.
- Push notifications need a relay reachable by every self-hosted server (decided at
  step 9, see the roadmap).

## Alternatives considered

- **App calling every service directly.** Keys would sit on the phone, there would be
  no background generation, the media server and request backend would need to be
  reachable from the phone, and multi-user would be harder. Rejected.
- **Hosted SaaS.** We would hold every user's history and keys, and pay for hosting.
  This goes against the self-hosted audience. Rejected.
- **Keep the feature inside the SuggestArr fork and point the app at it.** It would tie
  the app to a fork that must be rebased on every release, and to its single-thread
  WSGI-to-ASGI server. Rejected. The fork is kept unchanged until the app replaces it.
