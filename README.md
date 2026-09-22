

<img width="100%" alt="demo" src="https://github.com/user-attachments/assets/fa668bac-6399-4120-b034-1f5dc7e1289e" />



# Tindarr

**Swipe right on your next binge.**

Tired of scrolling Netflix for 45 minutes before giving up and rewatching The Office? Tindarr is Tinder for movies and series: AI-picked matches, one card at a time. Swipe right and the title goes straight to your request queue. Swipe left and it never has to know.

Under the hood, Tindarr is a self-hosted server plus an Android app (iOS is still playing hard to get). The server learns your type from your swipes and your media server's watch history, asks an AI provider to play matchmaker, and files requests through Seerr. It also serves a small web console for setup and administration. Pairing your phone takes one QR code scan, which is less awkward than asking for a number.

> **Relationship status: it's complicated.** Server foundation (step 1): the server starts and answers `/healthz` and `/api/v1/server/info`, but it's not ready to commit to anything usable yet. See the [roadmap](https://github.com/akira783/tindeerr/blob/main/docs/roadmap.md) and [server/README.md](https://github.com/akira783/tindeerr/blob/main/server/README.md).

## How it fits together

- **Media servers:** Jellyfin (10.10 or newer), Emby, Plex. Used to sign in (Tindeerr
  has no passwords of its own) and to read watch history.
- **Requests:** Seerr v3 (Jellyseerr still works as a legacy deployment; Overseerr
  only with Plex).
- **Metadata:** TMDb (required), OMDb (optional, for IMDb / Rotten Tomatoes ratings).
- **AI providers:** OpenAI, Anthropic, Google Gemini, Mistral, any OpenAI-compatible
  endpoint, Ollama.

Your data stays on your server. The AI provider you choose receives titles from your
votes and history, your taste profile and the mood you type. Nothing is sent to the
Tindeerr authors.

## Documentation

- [Architecture](docs/architecture.md)
- [Security model](docs/security.md)
- [Authentication, sessions and setup](docs/auth.md)
- [Roadmap](docs/roadmap.md)
- [Architecture decision records](docs/adr/)
- [HTTP API contract (OpenAPI 3.1)](api/openapi.yaml)

## Origin

The recommendation engine started as the "Swipe" feature of a fork of
[SuggestArr](https://github.com/giuseppe99barchetta/SuggestArr). See [NOTICE](NOTICE.md).

## License

[MIT](LICENSE)
