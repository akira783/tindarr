"""Ollama, over its own API rather than its OpenAI-compatible one.

Ollama has both, and the native one is better here: ``POST /api/chat`` takes ``format``
as a **JSON schema** and constrains generation to it, while the compatibility layer only
offers JSON mode. A local model needs that constraint more than a hosted one, not less.

It is also the only provider in this package with no SDK and no API key. The address is
the administrator's — the security model treats it exactly like the media server's
(section 7) — so it travels through the shared HTTP session, which follows no redirect,
bounds the body and times out like everything else.
"""

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, Final

import httpx2

from tindarr.adapters.http import (
    DEFAULT_TIMEOUT_S,
    NO_RESPONSE_REASONS,
    HttpSession,
    RemoteCallError,
    as_object,
    as_object_list,
    as_text,
    read_mapping,
)
from tindarr.adapters.llm.base import RawAnswer, StructuredProvider
from tindarr.adapters.llm.structured import (
    json_schema_of,
    messages_of,
    model_ids,
    problem_for,
)
from tindarr.ports import problems
from tindarr.ports.llm import (
    LlmCapabilities,
    LlmProviderKind,
    LlmUsage,
    Prompt,
    ReasoningEffort,
)

CHAT_PATH: Final = "/api/chat"
TAGS_PATH: Final = "/api/tags"
#: A local model on a laptop is slow; this is generous on purpose.
COMPLETION_TIMEOUT_S: Final = 300.0

logger = logging.getLogger(__name__)


class OllamaProvider(StructuredProvider):
    """The ``LlmProvider`` port over Ollama's native API."""

    kind: LlmProviderKind = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        reasoning_effort: ReasoningEffort | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(model, reasoning_effort)
        self.capabilities = LlmCapabilities(structured="json_schema", reasoning_effort=False)
        self._base_url = base_url
        self._transport = transport

    def _session(self, timeout_s: float) -> HttpSession:
        return HttpSession(
            self._base_url,
            headers={"Accept": "application/json"},
            timeout_s=timeout_s,
            transport=self._transport,
        )

    async def list_models(self) -> list[str]:
        """Return the models this Ollama has pulled (``GET /api/tags``)."""
        try:
            async with self._session(DEFAULT_TIMEOUT_S) as session:
                response = await session.request_bounded("GET", TAGS_PATH)
                if response.status_code != HTTPStatus.OK:
                    raise problem_for(response.status_code)
                payload = read_mapping(response)
        except RemoteCallError as failure:
            raise self._failure(failure) from None
        return model_ids(
            as_text(row.get("model")) or as_text(row.get("name"))
            for row in as_object_list(payload.get("models"))
        )

    async def complete(
        self, prompt: Prompt, schema: type[Any], retry_hint: str | None
    ) -> RawAnswer:
        """Send one chat request with the schema as ``format``."""
        options: dict[str, Any] = {"num_predict": self._max_output_tokens(prompt)}
        if prompt.temperature is not None:
            options["temperature"] = prompt.temperature
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": messages_of(prompt.instructions, retry_hint)},
                {"role": "user", "content": prompt.message},
            ],
            "format": json_schema_of(schema),
            "stream": False,
            "options": options,
        }
        try:
            async with self._session(COMPLETION_TIMEOUT_S) as session:
                response = await session.request_bounded("POST", CHAT_PATH, json_body=body)
                if response.status_code != HTTPStatus.OK:
                    raise problem_for(response.status_code)
                payload = read_mapping(response)
        except RemoteCallError as failure:
            raise self._failure(failure) from None
        return _answer_of(payload)

    @staticmethod
    def _failure(failure: RemoteCallError) -> Exception:
        logger.warning(
            "Ollama did not answer usably", extra={"llm": "ollama", "reason": failure.reason}
        )
        return problem_for(None, connection=failure.reason in NO_RESPONSE_REASONS)


def _answer_of(payload: Mapping[str, Any]) -> RawAnswer:
    """Read the assistant's text and the token counts out of one chat answer."""
    message = as_object(payload.get("message")) or {}
    text = as_text(message.get("content"))
    if text is None:
        logger.warning("Ollama answered with no content", extra={"llm": "ollama"})
        raise problems.llm_invalid_output()
    return RawAnswer(
        text,
        LlmUsage(
            input_tokens=_count(payload.get("prompt_eval_count")),
            output_tokens=_count(payload.get("eval_count")),
        ),
    )


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
