# Tindeerr

Swipe through AI-picked movies and series, one card at a time, and send the ones you
like straight to your request queue.

Tindeerr is a self-hosted server plus an Android app (iOS later). The server learns
your taste from your votes and your media server's watch history, asks an AI provider
for fitting titles, and files requests through Seerr / Jellyseerr / Overseerr.

> **Status:** design phase (step 0). Nothing to install yet. See the
> [roadmap](docs/roadmap.md).

## How it fits together

- **Media servers:** Jellyfin, Emby, Plex. Used to sign in and to read watch history.
- **Requests:** Seerr, Jellyseerr, Overseerr.
- **Metadata:** TMDb (required), OMDb (optional, for IMDb / Rotten Tomatoes ratings).
- **AI providers:** OpenAI, Anthropic, Google Gemini, Mistral, any OpenAI-compatible
  endpoint, Ollama.

Your data stays on your server. The AI provider you choose receives titles from your
votes and history, your taste profile and the mood you type. Nothing is sent to the
Tindeerr authors.

## Documentation

- [Architecture](docs/architecture.md)
- [Security model](docs/security.md)
- [Roadmap](docs/roadmap.md)
- [Architecture decision records](docs/adr/)
- [HTTP API contract (OpenAPI 3.1)](api/openapi.yaml)

## Origin

The recommendation engine started as the "Swipe" feature of a fork of
[SuggestArr](https://github.com/giuseppe99barchetta/SuggestArr). See [NOTICE](NOTICE.md).

## License

[MIT](LICENSE)
