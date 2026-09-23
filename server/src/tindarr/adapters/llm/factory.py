"""Mapping a stored AI connector to the adapter that answers for it.

Six kinds, four adapters. ``mistral`` and ``openai_compatible`` are the OpenAI adapter
with a different address — Mistral's is a preset so an administrator does not have to
know it — and ``openai`` is the same adapter with no address at all.

The composition root hands the result to the domain as an ``LlmProvider``; nothing else
knows which class answers for which kind.
"""

from http import HTTPStatus
from typing import Final

import httpx
import httpx2

from tindarr.adapters.llm.anthropic_provider import AnthropicProvider
from tindarr.adapters.llm.gemini_provider import GeminiProvider
from tindarr.adapters.llm.ollama_provider import OllamaProvider
from tindarr.adapters.llm.openai_provider import (
    MISTRAL_BASE_URL,
    OPENAI_BASE_URL,
    OpenAiProvider,
)
from tindarr.core.errors import ProblemError
from tindarr.ports.llm import LlmConnection, LlmProvider, LlmProviderFactory

#: Kinds that cannot work without an address of their own.
NEEDS_BASE_URL: Final = frozenset({"openai_compatible", "ollama"})
#: Kinds that cannot work without a key. Ollama has no accounts.
NEEDS_API_KEY: Final = frozenset({"openai", "anthropic", "gemini", "mistral"})
#: The OpenAI SDK refuses to be built without a key; a local gateway often needs none.
_NO_KEY: Final = "not-needed"


def _missing(field: str) -> ProblemError:
    return ProblemError(
        HTTPStatus.BAD_REQUEST, "validation_error", f"{field}: this AI provider needs it"
    )


def llm_provider_factory(
    transport: httpx2.AsyncBaseTransport | None = None,
    gemini_transport: httpx.AsyncBaseTransport | None = None,
) -> LlmProviderFactory:
    """Return the factory that builds a provider for a connection.

    Two transports, because ``google-genai`` carries its own HTTP client and therefore
    its own flavour of ``httpx``; in production both are ``None`` and each SDK opens its
    own connections.
    """

    def build(connection: LlmConnection) -> LlmProvider:
        if connection.kind in NEEDS_API_KEY and not connection.api_key:
            raise _missing("api_key")
        if connection.kind in NEEDS_BASE_URL and not connection.base_url:
            raise _missing("base_url")
        if connection.kind == "anthropic":
            return AnthropicProvider(
                connection.api_key,
                connection.model,
                reasoning_effort=connection.reasoning_effort,
                transport=transport,
            )
        if connection.kind == "gemini":
            return GeminiProvider(
                connection.api_key,
                connection.model,
                reasoning_effort=connection.reasoning_effort,
                transport=gemini_transport,
            )
        if connection.kind == "ollama":
            return OllamaProvider(
                connection.base_url or "",
                connection.model,
                reasoning_effort=connection.reasoning_effort,
                transport=transport,
            )
        return OpenAiProvider(
            connection.api_key or _NO_KEY,
            connection.model,
            kind=connection.kind,
            base_url=_openai_base_url(connection),
            reasoning_effort=connection.reasoning_effort,
            transport=transport,
        )

    return build


def _openai_base_url(connection: LlmConnection) -> str:
    if connection.kind == "mistral":
        return MISTRAL_BASE_URL
    if connection.kind == "openai_compatible":
        return connection.base_url or OPENAI_BASE_URL
    # An administrator must not be able to point the "openai" kind somewhere else by
    # leaving a stale base URL behind when they switch provider.
    return OPENAI_BASE_URL
