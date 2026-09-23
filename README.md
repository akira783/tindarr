<img width="100%" alt="demo" src="https://github.com/user-attachments/assets/fa668bac-6399-4120-b034-1f5dc7e1289e" />

# Tindarr

**Swipe right on your next binge.**

[![server](https://github.com/akira783/tindarr/actions/workflows/server.yml/badge.svg)](https://github.com/akira783/tindarr/actions/workflows/server.yml)
[![web](https://github.com/akira783/tindarr/actions/workflows/web.yml/badge.svg)](https://github.com/akira783/tindarr/actions/workflows/web.yml)
[![contract](https://github.com/akira783/tindarr/actions/workflows/contract.yml/badge.svg)](https://github.com/akira783/tindarr/actions/workflows/contract.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Tired of scrolling Netflix for 45 minutes before giving up and rewatching The Office? Tindarr is Tinder for movies and series: AI-picked matches, one card at a time. Swipe right and the title goes straight to your request queue. Swipe left and it never has to know.

Under the hood, Tindarr is a self-hosted server plus an Android app (iOS is still playing hard to get). The server learns your type from your swipes and your media server's watch history, asks an AI provider to play matchmaker, and files requests through Seerr. It also serves a small web console for setup and administration. Pairing your phone takes one QR code scan, which is less awkward than asking for a number.

## Relationship status: we've met, it's going well, no swiping yet

**What works today** (steps 1 and 2):

- **Claim your server** with a one-time setup code, from the web console it serves itself.
- **Point it at** Jellyfin (10.10+), Emby or Plex.
- **Sign in with your media server account** — a password, a Jellyfin Quick Connect code, or a Plex PIN. Tindarr has no passwords of its own, so it cannot leak yours.
- **Administer it**: server settings, users and roles, your own sessions, connector tests.
- **Pair a phone** by QR code, with an approval step, so nobody pairs theirs while you make coffee.

**What it still does not do** is the one thing it is named after: there is nothing to swipe. No TMDb, no AI, no requests, no app. Cards arrive at step 4, the app at step 7.

| Step | | |
|---|---|---|
| 1 | Server foundation | ✅ |
| 2 | Setup, sign-in, web console | ✅ |
| 3 | TMDb, OMDb, Seerr, AI providers | next |
| 4 | The swipe engine | |
| 5 | Packaged image, first real deployment | |
| 6–8 | Android app: pairing, the deck, likes and taste | |
| 9–11 | Offline votes, notifications, beta, release | |

The [roadmap](docs/roadmap.md) has the details, and every step ends with checks that must be green before the next one starts.

## How it fits together

```
 Android app (later)          Tindarr server (one per household)
        │  bearer token                │
        └───────────▶ /api/v1 ◀────────┤ web console (cookie + CSRF, same origin)
                         │
     ┌───────────────────┼───────────────────┬─────────────────┐
 Jellyfin / Emby / Plex  │      TMDb, OMDb   │   Seerr         │  AI provider
 sign-in, watch history  │      metadata     │   requests      │  the matchmaking
```

- **Media servers:** Jellyfin (10.10 or newer), Emby, Plex. Used to sign in and to read watch history.
- **Requests:** Seerr v3 (Jellyseerr still works as a legacy deployment; Overseerr only with Plex).
- **Metadata:** TMDb (required), OMDb (optional, for IMDb / Rotten Tomatoes ratings).
- **AI providers:** OpenAI, Anthropic, Google Gemini, Mistral, any OpenAI-compatible endpoint, Ollama.

Your data stays on your server. The AI provider you choose receives titles from your votes and history, your taste profile and the mood you type. Nothing is sent to the Tindarr authors: there is no Tindarr cloud, no telemetry, and nothing to sign up for.

## Try it before it can swipe

There is no published image yet — that is step 5. From a clone, with [uv](https://docs.astral.sh/uv/) and Node 24+:

```bash
(cd web && npm ci && npm run build)
cd server && TINDARR_WEB_DIR=../web/dist uv run tindarr serve   # http://127.0.0.1:8787
```

The first start prints the path to a one-time setup code (never the code itself: logs travel). Open the console, paste the code, and connect your media server. Over plain HTTP on a private network, add `TINDARR_ALLOW_HTTP_CONSOLE=true`; anywhere else, put it behind HTTPS. Every setting lives in [server/README.md](server/README.md).

## Security

Tindarr holds keys to your media server, your request queue and your AI provider's billing, so it is built accordingly: sessions revocable on the spot, rotating refresh tokens with reuse detection, secrets encrypted at rest and redacted from logs, strict CSP on the console, and rate limits that cannot be turned against you. The reasoning, the threat model and the parts that are deliberately not covered are in [docs/security.md](docs/security.md).

Found a hole? Report it privately through GitHub security advisories rather than an issue.

## Documentation

- [Architecture](docs/architecture.md) — layers, ports and adapters, storage, jobs
- [Security model](docs/security.md) — assets, adversaries, mitigations
- [Authentication, sessions and setup](docs/auth.md) — the reference the code follows
- [Evaluation harness](docs/evaluation.md) — how a recommendation strategy is measured before it ships
- [Roadmap](docs/roadmap.md) — what is done, what is next, and how each step is checked
- [Architecture decision records](docs/adr/) — every significant choice, with the alternatives
- [HTTP API contract (OpenAPI 3.1)](api/openapi.yaml) — written first, enforced in CI

## Origin

The recommendation engine started as the "Swipe" feature of a fork of
[SuggestArr](https://github.com/giuseppe99barchetta/SuggestArr). See [NOTICE](NOTICE.md).

## License

[MIT](LICENSE)
