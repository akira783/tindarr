# Deploying Tindarr

This is the guide for running Tindarr on your own machine, next to the media server it
already talks to. It assumes you have used Docker before and that somebody else's
reverse proxy is already terminating TLS on that host — or that you are willing to read
[the LAN-only section](#no-domain-name-the-lan-only-case) and accept what it costs.

Tindarr holds an administrator key to your media server, an API key that can fill your
disks through Seerr, and a key that bills your AI provider. The parts of this guide that
look paranoid are the parts that protect those three things. The reasoning behind each
one is in [the security model](security.md); the exact rules the code follows are in
[the authentication reference](auth.md).

## Before you start

| You need | Why | Where it goes |
|---|---|---|
| Jellyfin 10.10+, Emby or Plex, and an **administrator account** on it | Sign-in, watch history, and the account that claims the server | The setup wizard |
| A **TMDb** API key | Every card's poster, overview and metadata. Required. | The console, after setup |
| A **Seerr** instance (v3; Jellyseerr works as a legacy deployment, Overseerr only with Plex) | Where a swipe right files the request | The console |
| An **AI provider** | The matchmaking. OpenAI, Anthropic, Gemini, Mistral, any OpenAI-compatible endpoint, or Ollama. | The console |
| A **reverse proxy that terminates TLS**, and a name pointing at it | The console refuses to open a session over plain HTTP. | This guide |
| An **OMDb** key | Optional: IMDb and Rotten Tomatoes ratings on the cards. | The console |

Only the media server and the reverse proxy have to exist before the first start.
Everything else is typed into the console afterwards and can be changed there.

**There is no published image yet.** Roadmap step 5 puts signed multi-arch images on
GHCR; until the first release lands, [`../deploy/docker-compose.yml`](../deploy/docker-compose.yml)
builds one from a clone. That is the only difference: the built image is the same image.

## Starting it

```bash
git clone https://github.com/akira783/tindarr
cd tindarr
docker compose -f deploy/docker-compose.yml up -d --build
```

The build runs a Node stage for the console and a uv stage for the server; the runtime
image contains neither. It runs as UID/GID 10001 with a read-only root filesystem, no
capabilities and `no-new-privileges`, and writes only to the `/data` volume and a 64 MiB
tmpfs on `/tmp`. On a fresh start it settles at about 128 MiB of its 512 MiB cap and four
processes of its 256, and `docker compose ps` reports `healthy` roughly twenty seconds in.

The first start writes this, and nothing else worth reading:

```json
{"level": "INFO", "logger": "tindarr.main.app", "message": "secret key loaded", "key_id": "…", "source": "generated"}
{"level": "INFO", "logger": "tindarr.storage.migrate", "message": "database migrated", "from_revision": null, "to_revision": "0004"}
{"level": "INFO", "logger": "tindarr.auth.setup", "message": "setup required: enter the code from /data/setup-code in the web console"}
{"level": "INFO", "logger": "tindarr.main.app", "message": "tindarr started", "version": "0.1.0"}
```

Logs are JSON lines on stdout with credentials redacted, so shipping them to a log viewer
is safe. That is also why the setup code is not in them.

### The setup code

A server nobody has claimed writes a one-time code to `/data/setup-code` — twelve
Crockford base32 characters, sixty bits of randomness, mode 0600 — and logs **only the
path**. Read it from the container:

```bash
docker compose -f deploy/docker-compose.yml exec tindarr cat /data/setup-code
```

Open the console, paste the code, and the browser that claimed it holds a thirty-minute
setup session. Only that browser can finish: setup completes at the first console sign-in
that carries the setup session cookie **and** authenticates a user who is an administrator
on the configured media server. The code survives restarts and stays valid until then; a
second claim revokes the first, so there is only ever one setup session alive. When setup
completes the file is deleted and the code stops working.

If you lose the browser mid-setup, claim again with the same code. If you lose the code,
delete `/data/setup-code` and restart: a new one is generated. If the media server itself
was replaced and nobody can authenticate on the old one any more, `tindarr media-server
reset --yes` inside the container clears the connector, revokes every session, unlinks
every user — their votes and history stay — and writes a fresh setup code.

## Behind a reverse proxy

### Why the console insists on HTTPS

Endpoints that set a cookie answer `403` with `code = https_required` unless the request
arrived over HTTPS, or came from loopback to a `localhost`/`127.0.0.1`/`[::1]` host, or
`TINDARR_ALLOW_HTTP_CONSOLE` is on and the client address is private:

```json
{"type":"about:blank","title":"Forbidden","status":403,"code":"https_required",
 "detail":"The console needs HTTPS, except on localhost or with TINDARR_ALLOW_HTTP_CONSOLE from a private address."}
```

The session cookie is `__Host-tindarr_session`, and the `__Host-` prefix is not decoration:
it makes the cookie unsettable by a sibling subdomain and refused by the browser without
`Secure`. A console reachable over plain HTTP is a console whose cookies a neighbour on the
same network can read off the wire, along with the CSRF token and every request that
carries the media server's key.

### What the proxy must pass through

Three headers matter, and one that people expect to matter does not.

- **`Host`**, unchanged. Tindarr builds the origin it checks CSRF against out of the `Host`
  header. Traefik and Caddy pass the original by default; nginx needs
  `proxy_set_header Host $host` written out.
- **`X-Forwarded-Proto`**, so the server knows the leg it cannot see was TLS. Without it,
  every request looks like plain HTTP and sign-in answers `https_required`.
- **`X-Forwarded-For`**, so rate limits, the `lan_only` password rule and the remote-access
  rule apply to the real client rather than to the proxy.
- **`X-Forwarded-Host` and `X-Forwarded-Port` are ignored unconditionally**, trusted proxy
  or not. There is no configuration that turns them on. A proxy that rewrites `Host` and
  puts the original in `X-Forwarded-Host` will get `host_not_allowed`; fix the proxy.

The console opens no websocket and consumes no event stream, so no `Upgrade` handling is
needed.

### Naming the host the server answers to

Before routing, every request except `/healthz` must carry a `Host` the server accepts: an
IP literal, `localhost`, a name in `TINDARR_ALLOWED_HOSTS`, or the host of `public_url`.
Anything else gets

```json
{"type":"about:blank","title":"Bad Request","status":400,"code":"host_not_allowed",
 "detail":"This server does not answer to that host name; see TINDARR_ALLOWED_HOSTS."}
```

This is what stops DNS rebinding: a page on an attacker's domain that resolves to your LAN
address reaches the port, and is refused before it reaches a route. The cost is that a
server first reached by a domain name needs the name configured **before that first
request**, or it answers 400 to its own console. Set `TINDARR_PUBLIC_URL` — the whole
origin, `https://tindarr.example.com` — and its host becomes an allowed host. Use
`TINDARR_ALLOWED_HOSTS` (comma-separated names, no scheme, no port, no wildcards; the
server refuses to start on a malformed entry) when the server answers to more than one
name, or when you want the name accepted without pinning `public_url` yet.

`TINDARR_PUBLIC_URL` is the address phones are pointed at: it is what a pairing QR code
carries, and pairing refuses to start without it. Set from the console it is verified
first — the server fetches its own `server/info` through that URL and checks an HMAC bound
to the host the request arrived at, which is why a proxy in front of this same instance
passes and another server does not. Set as an environment variable it is **locked**: the
console shows it as locked and cannot change it, and the check runs once after startup and
only logs on failure. That is the escape hatch for a host with no NAT hairpinning, which
cannot reach its own public address to prove anything.

### `TINDARR_TRUSTED_PROXIES`: one address, never a range

`X-Forwarded-For`, `X-Forwarded-Proto`, `Forwarded` and `X-Request-ID` are ignored from
anyone not listed here. When the list is empty, every client behind the proxy shares the
proxy's address: limits get stricter, never looser, and a private peer that sends forwarded
headers is warned about once an hour —

```json
{"level":"WARNING","logger":"tindarr.api.context","peer":"172.25.0.1",
 "message":"forwarded headers from an untrusted peer: they are ignored. If this is your reverse proxy, add it to TINDARR_TRUSTED_PROXIES"}
```

— which is the log line that tells you the setting is missing.

**List the proxy's own address. Not the network it sits on.** Everything in that list may
claim to be any client on any scheme. It may claim `127.0.0.1`, which is both loopback and
private, and so passes the "private network" test that `password_sign_in: lan_only`, the
remote-access rule and `TINDARR_ALLOW_HTTP_CONSOLE` are decided on. It may claim `https`,
which is what `https_required` is decided on. Writing `172.16.0.0/12` hands all of that to
every container on every Docker bridge network on the host — including the one you will add
next month for something you downloaded. The value is parsed as IPs or CIDRs, so a `/32`
is accepted and so is a bare address; a host name is refused at startup.

Find the address of the proxy container:

```bash
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' traefik
```

Pin it, and remember that it changes when the proxy's network is recreated. A proxy on the
host rather than in a container is the Docker bridge gateway, which `docker inspect` prints
as `.Gateway` on the Tindarr container.

When several hops are listed, the client IP is the **rightmost entry that is not itself a
trusted proxy** — walking from the right and skipping trusted addresses, so a client that
prepends a fake entry only lengthens the part that gets skipped. If every hop is trusted,
the leftmost is used. A hop that is not a valid IP address makes the server fall back to the
proxy's own address and log that the proxy is misconfigured.

### Traefik

Give Tindarr the proxy's network, drop the published port, and let Traefik route to 8787.
Traefik preserves the original `Host` and sets the `X-Forwarded-*` headers itself.

```yaml
services:
  tindarr:
    # everything from deploy/docker-compose.yml, minus the `ports:` block
    networks: [proxy]
    environment:
      TINDARR_PUBLIC_URL: https://tindarr.example.com
      TINDARR_TRUSTED_PROXIES: 172.19.0.5/32   # the traefik container, /32
      TINDARR_HSTS: "true"                     # only when it is always HTTPS
    labels:
      traefik.enable: "true"
      traefik.docker.network: proxy
      traefik.http.routers.tindarr.rule: Host(`tindarr.example.com`)
      traefik.http.routers.tindarr.entrypoints: websecure
      traefik.http.routers.tindarr.tls.certresolver: letsencrypt
      traefik.http.services.tindarr.loadbalancer.server.port: "8787"

networks:
  proxy:
    external: true
```

Traefik's own `forwardedHeaders.trustedIPs` is a separate question: it decides which
`X-Forwarded-For` Traefik itself believes. Leave it alone unless something sits in front of
Traefik, and if something does, name that one address there too.

### Caddy

Caddy terminates TLS on its own, passes the original `Host` upstream and sets the forwarded
headers. The whole configuration is two lines:

```caddyfile
tindarr.example.com {
	reverse_proxy tindarr:8787
}
```

with `TINDARR_PUBLIC_URL: https://tindarr.example.com` and `TINDARR_TRUSTED_PROXIES` set to
the Caddy container's address on the shared network.

### nginx

nginx forwards nothing by default, so every header is written out:

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name tindarr.example.com;

    ssl_certificate     /etc/letsencrypt/live/tindarr.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/tindarr.example.com/privkey.pem;

    # Poster grids and a taste profile are small; an import is not.
    client_max_body_size 16m;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`$proxy_add_x_forwarded_for` appends the peer to whatever `X-Forwarded-For` arrived, so a
client can put anything it likes to the left of the real address. That is fine, and it is
exactly why the rightmost-untrusted-hop rule exists — but it is also why
`TINDARR_TRUSTED_PROXIES` must name nginx and nothing wider. With nginx on the host, the
compose file's `127.0.0.1:8787` publication is the right one and the trusted address is the
Docker bridge gateway.

### Checking that it worked

From the proxy's point of view, not yours:

```bash
curl -s https://tindarr.example.com/api/v1/server/info
```

A JSON body with `"setup_required"` means the `Host` was accepted. `host_not_allowed` means
`TINDARR_PUBLIC_URL` or `TINDARR_ALLOWED_HOSTS` is missing. Then sign in: if the cookie in
the response carries `__Host-` and `Secure`, `X-Forwarded-Proto` arrived and the proxy is
trusted. If sign-in answers `https_required` over an HTTPS URL, one of those two is wrong —
almost always the trusted-proxies address.

## No domain name: the LAN-only case

`TINDARR_ALLOW_HTTP_CONSOLE=true` lets the console open a session over plain HTTP, but only
when the resolved client address is inside `127.0.0.0/8`, `::1/128`, `10.0.0.0/8`,
`172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, `fc00::/7` or `fe80::/10`. The server
logs a warning at every start:

```json
{"level":"WARNING","logger":"tindarr.main.app",
 "message":"TINDARR_ALLOW_HTTP_CONSOLE is on: console sessions are accepted over plain HTTP from private addresses, without Secure cookies"}
```

What it costs, plainly: the cookies lose the `__Host-` prefix and become `tindarr_session`,
`tindarr_setup` and `tindarr_preauth`, and they lose `Secure`. `HttpOnly` and
`SameSite=Strict` stay. Session token, CSRF token and every admin request cross your LAN in
clear text. Anyone who can see that traffic — another device on the Wi-Fi, a guest, a
compromised television — can take the admin session and with it the media server's API key
and the AI provider's billing. This is a reasonable trade on a wired home network you
control. It is not a reasonable trade on shared or guest Wi-Fi.

Two things to know before you rely on it:

**Loopback needs nothing.** A browser on the same machine reaching `http://localhost:8787`
already gets `__Host-` cookies with `Secure`, because browsers treat loopback as a secure
context. `TINDARR_ALLOW_HTTP_CONSOLE` is for *other* devices on the LAN.

**Tailscale is not private.** The carrier-grade NAT range `100.64.0.0/10`, which Tailscale
addresses come from, is deliberately not in the private list. Reaching the console over
Tailscale therefore does not qualify for the plain-HTTP exception, and it does not need to:
Tailscale is already encrypted, and putting a TLS name in front of it is easier than
arguing with this. The same goes for any other WireGuard-style tunnel — which, if you want
the honest recommendation, is the better answer to "I have no domain name" than plain HTTP
ever is.

To serve other devices, the published port has to leave loopback. Publish it on one
address, not on all of them:

```yaml
    ports:
      - "192.168.1.10:8787:8787"
```

Docker's published ports are inserted ahead of the host firewall, so `0.0.0.0:8787:8787`
is reachable from anywhere the host is, whatever ufw says.

## Backups

### What is in `/data`

| File | What it is | Backup |
|---|---|---|
| `tindarr.db` | SQLite: users, sessions, votes, taste profiles, history, settings with their secrets encrypted | Yes |
| `tindarr.db-wal`, `tindarr.db-shm` | SQLite's write-ahead log and shared memory, live while the server runs | Only as part of a consistent copy |
| `secret.key` | The master key, 0600, generated on first start when `TINDARR_SECRET_KEY` is unset | Yes, **separately** |
| `setup-code` | The one-time claim code, deleted at setup completion | No |
| `backups/` | Pre-migration copies the server takes itself | Convenient, not a backup |
| `.migrate.lock` | Serialises migrations between processes | No |

### Why the database alone is worth nothing

Every secret in the `settings` table — the media server's API key, the Seerr key, the AI
provider's key, the Plex owner token — is encrypted with AES-256-GCM under a key derived
from `secret.key`. The JWT signing key comes from the same material under its own label, so
a restore with a different key signs everybody out as a side effect. A database restored
without its key gives you your votes and your users and not one working connector.

So back up both, and **not into the same archive**: the point of separating them is that
whoever ends up holding your off-site backup does not hold a decryptable copy of four API
keys. A key in a password manager and a database in restic is a good split. Both in the
same restic repository is the split that does nothing.

### Holding the key yourself

Supplying the key means it never has to be extracted from a volume:

```bash
openssl rand -base64 32 > deploy/secret_key.txt   # 32 characters minimum
chmod 600 deploy/secret_key.txt
```

then uncomment the three blocks the compose file already carries — `TINDARR_SECRET_KEY_FILE`,
the service's `secrets:` list, and the top-level `secrets:` definition. The file is mounted
at `/run/secrets/tindarr_secret_key` and read from there; `/data/secret.key` is never
created. Every `TINDARR_*` variable accepts this `_FILE` form, and when both are set the
file wins.

Do this before the first start. Doing it afterwards replaces a key that already encrypted
things, and the server will start fine and then fail to decrypt every connector it holds.

### Taking a copy

Stop the server and copy the directory. SQLite in WAL mode does not promise a consistent
copy of a live database taken with `cp`:

```bash
docker compose -f deploy/docker-compose.yml stop
docker run --rm -v tindarr_tindarr-data:/data -v "$PWD:/out" alpine \
    tar czf /out/tindarr-$(date -u +%Y%m%dT%H%M%SZ).tar.gz -C /data .
docker compose -f deploy/docker-compose.yml start
```

That is a few seconds of downtime for a server nobody is swiping on at four in the morning.
If you refuse the stop, `sqlite3 /data/tindarr.db ".backup /out/tindarr.db"` takes a
consistent online copy of the database — and then you still have to store `secret.key`
somewhere, which is the part that matters.

### Restoring

The procedure, and the three things that bite:

```bash
docker compose -f deploy/docker-compose.yml down                    # not -v
docker volume rm tindarr_tindarr-data && docker volume create tindarr_tindarr-data
docker run --rm -v tindarr_tindarr-data:/data -v "$PWD:/in" alpine sh -c '
    tar xzf /in/tindarr-20260101T040000Z.tar.gz -C /data &&
    chown -R 10001:10001 /data && chmod 700 /data && chmod 600 /data/secret.key'
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml logs -f
```

1. **Ownership.** The image runs as UID 10001 and the data directory is 0700. A restore
   that leaves files owned by root gives a server that cannot open its own database.
2. **The key's mode.** The server refuses to start on a `secret.key` that is readable by
   group or others, and says so: `SecretKeyError: …; run 'chmod 600 /data/secret.key' and
   restart`. With `restart: unless-stopped` it will loop on that until you fix it —
   `docker compose logs` is where you find out, because the container's own status just
   says it keeps restarting. Every `tar` and every `cp` is a chance to lose that mode.
3. **The revision.** Restoring an old database into a newer image migrates it forward on
   the next start, which is fine and is what the pre-migration backup exists for. Restoring
   a *newer* database into an older image does not: the server refuses with "database
   revision … is unknown to this version … Upgrade the server, or restore a backup from
   before that upgrade."

Then verify it actually came back, rather than assuming: sign in to the console and open
the connectors page. Secrets show as `set` plus their last four characters. If the media
server, Seerr and the AI provider all read `set`, the key matched the database. If they
read as unset, it did not — stop, and go and find the right `secret.key` before somebody
retypes four API keys over a restore that was recoverable.

## Updating

```bash
cd tindarr && git pull
docker compose -f deploy/docker-compose.yml up -d --build
```

After the first GHCR release, that becomes `docker compose pull && docker compose up -d`
against a pinned tag. Pin one. `latest` turns every pull into an upgrade nobody reviewed,
on a server that holds your media server's admin key.

Migrations run at startup, before the server accepts anything. When the database is already
at the newest revision, nothing happens. When it is not, the server first copies it to
`/data/backups/tindarr-<timestamp>-rev-<revision>.db` — a self-contained file, mode 0600,
verified with `PRAGMA integrity_check` before the migration is allowed to start — and keeps
the last `TINDARR_DB_BACKUPS_KEEP` of them, five by default. Then it migrates inside one
transaction and logs `database migrated` with the revisions it moved between.

If the migration fails, the server does not start; it exits on the error and `restart:
unless-stopped` retries it. Read the logs, not the container status. Going back is a matter
of putting the old image back and restoring the pre-migration backup the new one left
behind — which is a plain SQLite file with no `-wal` companion, so restoring it means
copying it over `tindarr.db` with the server stopped and deleting the stale `tindarr.db-wal`
and `tindarr.db-shm` next to it. Keep `secret.key` exactly where it was: an update never
changes it.

Read the release notes before a major version. Step 5 is where images start being published
with an SBOM, a Trivy scan and a cosign signature; verifying the signature before pulling is
worth the thirty seconds once the tooling documents itself.

## Watching it

There is exactly one endpoint to point a monitor at:

```
GET /healthz  →  200  {"status":"ok"}
```

No authentication, and it is the one path exempt from the allowed-hosts check, so a probe
that reaches it by IP address works without listing that address anywhere. It is also the
one path whose successful calls are logged at `DEBUG` rather than `INFO`, so a monitor
polling it every thirty seconds does not drown the log.

**Know what it proves.** It is a liveness probe and nothing more: it touches neither the
database nor the network, and answers 200 as long as the process is up and serving. It will
say `ok` while TMDb is down, while the media server is unreachable, and while the AI
provider is refusing the key. There is no readiness endpoint and there is no `/metrics`; if
you want to know that the parts outside the process are healthy, watch those parts.

The image carries its own healthcheck, `tindarr healthcheck`, which requests
`/healthz` on the configured host and port — substituting `127.0.0.1` for a wildcard bind —
with a three-second timeout, ignoring `HTTP_PROXY` and `HTTPS_PROXY`, and exits 0 only on a
literal 200. `docker compose ps` reports `healthy` about twenty seconds after start. A
healthcheck should treat exactly that as healthy and everything else as not: there is no
degraded state to interpret.

For Uptime Kuma or anything like it, an HTTP monitor on `https://tindarr.example.com/healthz`
expecting 200 covers the process, the proxy, the certificate and DNS in one check — which is
more than the endpoint itself knows. Two log lines are worth an alert of their own:
`forwarded headers from an untrusted peer`, which means `TINDARR_TRUSTED_PROXIES` no longer
matches the proxy's address after a network was recreated, and `Application startup failed`,
which with `restart: unless-stopped` is otherwise just a container that quietly keeps
restarting.

## Every setting

The full table — defaults, meanings, and which ones lock a console setting when they are set
— is in [`../server/README.md`](../server/README.md#configuration). The ones this guide uses:
`TINDARR_PUBLIC_URL`, `TINDARR_ALLOWED_HOSTS`, `TINDARR_TRUSTED_PROXIES`,
`TINDARR_ALLOW_HTTP_CONSOLE`, `TINDARR_HSTS`, `TINDARR_SECRET_KEY` (and its `_FILE` form),
`TINDARR_LOG_LEVEL` and `TINDARR_DB_BACKUPS_KEEP`.
