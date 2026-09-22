# Tindeerr server

Python package behind the Tindeerr app. It implements the HTTP contract in
[`../api/openapi.yaml`](../api/openapi.yaml); see [architecture](../docs/architecture.md)
and [security](../docs/security.md) for the design.

## Development

Requirements: [uv](https://docs.astral.sh/uv/). uv installs the Python it needs
(3.12 or newer; `.python-version` pins 3.13 for development).

```sh
cd server
uv sync                      # create .venv from uv.lock
uv run tindeerr              # serve on http://127.0.0.1:8787, data in ./data
```

Checks (the same ones CI runs):

```sh
uv run ruff check .          # lint
uv run ruff format --check . # formatting (drop --check to fix)
uv run pyright               # types, strict mode
uv run lint-imports          # layer rules (pyproject.toml, [tool.importlinter])
uv run pytest --cov          # tests, coverage must stay >= 95 %
```

Dependencies are locked in `uv.lock` (with hashes). Add one with `uv add <package>`
(or `uv add --group dev <package>`), and commit the lock file.

### Migrations

The server migrates its database at startup, after taking a backup of an existing
database into `<data>/backups/` (the last `TINDEERR_DB_BACKUPS_KEEP` are kept). To add
a revision, change `src/tindeerr/storage/tables.py`, then:

```sh
uv run alembic upgrade head                                   # dev database at head
uv run alembic revision --autogenerate --rev-id 0002 -m "add users"
```

Review the generated file: a test fails if the tables and the migrated schema differ.

## Configuration

Everything is read from the environment. Each variable can instead point to a file
with the `_FILE` suffix (Docker secrets); **the file wins** when both are set. Empty
values count as unset.

| Variable | Default | Meaning |
|---|---|---|
| `TINDEERR_DATA_DIR` | `data` (`/data` in the image) | Database, backups and generated key. |
| `TINDEERR_SECRET_KEY` | generated | Master key, 32+ characters. When unset, a random key is written once to `<data>/secret.key` (mode 0600). The server refuses to start if that file is readable by group or others. Changing the key makes stored secrets unreadable. |
| `TINDEERR_HOST` | `127.0.0.1` (`0.0.0.0` in the image) | Listening address. |
| `TINDEERR_PORT` | `8787` | Listening port. |
| `TINDEERR_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. Logs are JSON lines on stdout, with credentials redacted. |
| `TINDEERR_TRUSTED_PROXIES` | none | Comma-separated IPs/CIDRs of reverse proxies. `X-Forwarded-For` / `-Proto` are ignored from anyone else, and so is `X-Request-ID`. A private address sending forwarded headers without being listed is logged once per hour. |
| `TINDEERR_ALLOWED_HOSTS` | none | Comma-separated host names accepted in the `Host` header, besides IP literals, `localhost` and the host of `public_url`. Anything else gets `400 host_not_allowed` (except `/healthz`), which is what stops DNS rebinding. A server reached by a domain name needs this or `TINDEERR_PUBLIC_URL`. |
| `TINDEERR_ALLOW_HTTP_CONSOLE` | `false` | Accept console sessions over plain HTTP from private client addresses. The cookies then lose the `__Host-` prefix and `Secure`; a warning is logged at startup. Without it, only HTTPS (or `localhost` from the same machine) can sign in. |
| `TINDEERR_API_DOCS` | `false` | Serve interactive docs at `/api/docs` (and `/api/openapi.json`). |
| `TINDEERR_HSTS` | `false` | Send `Strict-Transport-Security: max-age=31536000`. Only enable it when the server is always reached over HTTPS. |
| `TINDEERR_DB_BACKUPS_KEEP` | `5` | Pre-migration backups to keep. |

Settings stored in the database can be forced the same way; they then show as locked
in the web console. Today: `TINDEERR_SERVER_NAME`, `TINDEERR_MEDIA_SERVER_KIND`
(`jellyfin`, `emby`, `plex`), `TINDEERR_MEDIA_SERVER_URL`,
`TINDEERR_MEDIA_SERVER_API_KEY`, `TINDEERR_MEDIA_SERVER_VERIFY_TLS`,
`TINDEERR_PUBLIC_URL`, `TINDEERR_PASSWORD_SIGN_IN` (`enabled`, `lan_only`, `disabled`),
and the ones the swipe engine will use from step 4 (`TINDEERR_LANGUAGE`,
`TINDEERR_STREAMING_REGION`, `TINDEERR_DAILY_GENERATION_LIMIT`,
`TINDEERR_WARM_UP_ENABLED`, `TINDEERR_CONTENT_FILTERS`). Values are checked against the setting's type
(`true`/`false` for booleans, JSON for lists and objects); an invalid one stops the
server at startup with a message naming the variable.

## First run

A server that has not been set up writes a one-time setup code to `<data>/setup-code`
(mode 0600) and logs **its path only**. The code is entered in the web console, which
claims the server; the same code stays valid across restarts until setup completes.
Deleting the file and restarting generates a new one. The whole flow is described in
[the authentication reference](../docs/auth.md).

## Container

```sh
docker build -t tindeerr-server server/
docker run -d --name tindeerr -p 8787:8787 -v tindeerr-data:/data \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  tindeerr-server
```

The image runs as UID/GID 10001 and only writes to `/data` (a bind mount must be
writable by that UID) and `/tmp`. See [`../deploy/docker-compose.yml`](../deploy/docker-compose.yml).
Back up the volume and the key separately.
