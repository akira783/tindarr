# 0005. Ports and adapters for external systems; AI providers without a gateway library

- Status: accepted
- Date: 2026-09-22

## Context

A public release must support Jellyfin, Emby and Plex, the Seerr family, TMDb/OMDb,
and several AI providers (OpenAI, Anthropic, Google Gemini, Mistral, OpenAI-compatible
endpoints, Ollama). More will be asked for (Radarr/Sonarr direct, Trakt, other LLMs).

## Decision

- The domain only talks to `Protocol` ports (`MediaServer`, `RequestBackend`,
  `Metadata`, `LlmProvider`). Each external system is one adapter package with its
  own contract test suite, run against recorded responses (and optionally against
  real instances).
- **Media servers.**
  - Jellyfin and Emby share most of one adapter, with small overrides.
  - Plex has its own adapter: X-Plex-Token, plex.tv PIN, the owner token for history.
- **Request backend.** One adapter for Seerr, Jellyseerr and Overseerr (same API).
  Requests are sent with the admin API key and `X-API-User: <backend user id>`, which
  all three honour: the request is then made as that user, with their permissions,
  quotas, auto-approval and override rules. Backend users are matched by their
  `jellyfinUserId` (Jellyfin, Emby) or `plexId`. Series are requested for all seasons
  or the first one only, as the admin chooses. Radarr/Sonarr direct may come later
  behind the same port.
- **AI providers**, with official SDKs only:
  - **OpenAI:** `openai`, structured outputs (JSON schema).
  - **Anthropic:** `anthropic`, forced tool use carrying the JSON schema.
  - **Google Gemini:** `google-genai`, `response_schema`.
  - **Mistral:** its OpenAI-compatible API through the OpenAI adapter, as a preset.
  - **OpenAI-compatible (generic):** base URL + key. This covers OpenRouter, Groq,
    LM Studio, vLLM, local proxies.
  - **Ollama:** its native API `/api/chat` with `format` = JSON schema. The model list
    comes from `/api/tags`.
- Each provider declares its capabilities (`json_schema`, `json_mode`, `text`). The
  shared layer adds what the fork already does well: JSON extraction and repair,
  Pydantic validation, one retry with the validation error, and error mapping to
  stable codes (`llm_auth_failed`, `llm_quota`, `llm_model_not_found`,
  `llm_unreachable`, `llm_invalid_output`).
- **Model ids are never hard-coded.** The admin picks one from the provider's live
  model list, or types one.

## Consequences

- New integrations do not touch the engine, and a broken provider cannot break the
  others.
- Three SDKs to keep updated (Renovate).
- Behaviour differences between providers are absorbed by the validation and retry
  layer. Prompts stay provider-neutral.

## Alternatives considered

- **LiteLLM or a similar gateway.** Wide coverage, but a heavy, fast-moving dependency
  for a single call type, and a larger supply-chain surface. Rejected.
- **OpenAI-compatible only.** Anthropic's and Gemini's compatibility layers lag behind
  on structured output. Rejected, but it stays the generic fallback.
