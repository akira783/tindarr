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
