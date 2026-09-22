# 0006. Contract-first HTTP API, versioned, with problem details

- Status: accepted
- Date: 2026-09-22

## Context

The app and the server are released separately, and self-hosted servers update on
their owners' schedule. A phone may talk to an older or newer server.

## Decision

- **Source of truth.** `api/openapi.yaml` (OpenAPI 3.1) is written first. The server
  implements it, and the app client is generated from it. CI validates the server's
  real responses against the committed schemas and rejects any route missing from the
  contract (a byte-for-byte comparison of generated specs was dropped as too brittle).
- **Versioning.** Every endpoint lives under `/api/v1`. Inside v1, changes are additive
  only: new endpoints, new optional fields, new enum values. Clients must ignore
  unknown fields and treat unknown enum values gracefully. A breaking change means
  `/api/v2`, served alongside v1 for at least one minor release.
- **Negotiation.** `GET /api/v1/server/info` returns `version`, `api_version`,
  `min_app_version`, `auth_methods` (`password`, `plex_pin`) and `capabilities`
  (strings such as `trailers`, `ratings`, `notifications`). The app enables features
  by capability and asks the user to update when either side is too old.
- **Errors.** `application/problem+json` (RFC 9457) with a stable `code`. The app
  switches on `code`, never on the message text.
- **Pagination.** Opaque cursors (`cursor`, `next_cursor`).
- **Idempotency.** Votes carry a client-generated `client_vote_id`, so the offline
  queue can resend safely.
- **Slow work.** Returns `202` with `retry_after_ms`, and the client polls. SSE or
  websockets can come later, behind a capability, without changing the flows.

## Consequences

- Both sides can evolve independently, and mismatches show up in CI rather than on a
  user's phone.
- Writing the contract before the code costs time up front, and saves it at every
  integration.
