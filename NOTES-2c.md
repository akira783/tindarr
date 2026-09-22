# Notes for lot 2c (from the web console, lot 2d) — delete this file when 2c is done

## The server must, for the console to work

- Serve `TINDEERR_WEB_DIR` (default `/app/web`) under `/`.
- `index.html` fallback for every GET/HEAD outside `/api` and `/healthz`.
- `/assets/…` immutable cache, real 404 (no fallback there).
- Headers per path — index: `no-store`, the console CSP, COOP `same-origin`,
  `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`,
  CORP `same-origin`.
- The smoke test's "`/` serves index.html under the console CSP" assertion turns
  itself on as soon as `/` answers 200.

## Contract friction found while building the console (not changed by 2d)

1. `SetupState` does not expose the locked values (`url`, `server_type`) although
   `PUT /setup/media-server` requires them: the wizard can only say "must match".
   → Either expose the locked values in `SetupState`, or let the PUT omit them when
   they are locked.
2. `verify_tls` has a `default`, so the generated TypeScript type makes it required
   while it must be omitted when locked (worked around with a cast in
   `MediaServerStep`).
3. `GET /pairings/{id}` returns no `retry_after_ms`, so the console polls at a fixed
   2 s.

## Contract and documentation friction found while building lot 2b

4. `SetupState.media_server` documents an optional `name`, but nothing gives the
   wizard the media server's own name: the connection test returns it in
   `ConnectorStatus.server_name`, which `GET /setup/state` does not carry. The
   server answers `{"kind": …}` only. → Either drop `name` from the contract or
   store the tested server's name so the wizard can show "connected to Home
   Jellyfin".
5. `Connector.secret.last4` is filled with the last four characters of the stored
   secret (admin-only, and the contract defines the field for telling keys apart).
   If that is judged too much disclosure for a Plex **account** token, 2c should
   say so and the server can return null.
6. `GET /admin/connectors` and `DELETE /admin/connectors/{kind}` are not
   implemented: 2b only needed `PUT /admin/connectors/media_server` and its
   `test`. Every other kind answers `404 not_found`.
7. `ServerInfo.auth_methods` gains `quick_connect` only once the background probe
   has read `GET /QuickConnect/Enabled` (15 s after startup, then every 5 min), so
   a console loaded in the first seconds of a restart sees only `password`. The
   sign-in page should re-read `server/info` when it is shown, not once per boot.
8. `docs/architecture.md` sketches `PlexTv.create_pin(self, client_id)` and a
   `MediaServer` port without `quick_connect_enabled`. Both moved: `create_pin`
   also takes the device name that names Tindeerr on the plex.tv approval page
   (docs/auth.md §4 requires it), and `quick_connect_enabled` is what feeds the
   cache `server/info` reads. The sketch should be updated with the rest of step 2.
9. `POST /admin/connectors/{kind}/test` can answer `403 plex_owner_required` — a
   Plex owner token that does not own the server is a refusal an administrator
   must see, and `PUT /setup/media-server` and `PUT /admin/connectors/{kind}`
   both document it. `testConnector` documents only `AdminForbidden`
   (`admin_required`, `csrf_failed`, `remote_access_denied`). → Add
   `plex_owner_required` to its `403`.
10. `POST /auth/plex/login` and `POST /auth/web/plex/login` can answer `503`
    `media_server_unreachable`: the Plex server's own identity is re-read before
    the sign-in uses it (docs/auth.md §6), which the contract's `503` list
    (`setup_required`, `plex_tv_unreachable`, `media_server_changed`) does not
    mention. → Add it, or say in §6 that a Plex sign-in skips the identity check.
11. **docs/auth.md §6 says `GET /Users` with the API key "proves the key is an
    admin key". It does not.** Jellyfin answers `200` to any authenticated caller
    and filters the list in place, so a *user* access token pasted into the API key
    field passes the connection test, and the hourly sync then reads that filtered
    list as "these users were removed". Lot 2b added a guard against the worst of
    it (a list with no users at all changes nothing), but the claim itself is
    wrong. → Either probe something actually elevation-gated
    (`GET /System/Configuration`, `GET /Auth/Keys`) or soften §6, and decide it
    before step 3 makes the sync authoritative for more things.
