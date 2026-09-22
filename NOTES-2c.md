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
