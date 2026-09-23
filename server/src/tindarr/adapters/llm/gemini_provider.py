"""Google Gemini, through ``google-genai`` and its ``response_schema``.

Gemini takes the Pydantic model itself as ``response_schema`` and answers with JSON that
fits it, which is the strictest of the four arrangements in this package and needs the
least code. The answer still goes through the shared repair-and-validate layer: a
provider that promises a schema and returns something else is exactly the case that
layer exists for, and the retry has to behave the same way everywhere.

This is the one adapter that does not talk through ``httpx2``: ``google-genai`` carries
its own HTTP client, so the tests inject a transport through ``HttpOptions`` rather than
through the shared session. Timeouts there are in **milliseconds**, which is worth
saying out loud once.
"""

import logging
from typing import Any, Final, cast

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from tindarr.adapters.http import DEFAULT_TIMEOUT_S
from tindarr.adapters.llm.base import RawAnswer, StructuredProvider
from tindarr.adapters.llm.structured import messages_of, problem_for
from tindarr.ports import problems
from tindarr.ports.llm import (
    LlmCapabilities,
    LlmProviderKind,
    LlmUsage,
    Prompt,
    ReasoningEffort,
)

#: Gemini names its models ``models/gemini-…``; the model field takes the bare id.
_MODEL_PREFIX: Final = "models/"
#: What a model has to be able to do to be worth offering in the picker.
_GENERATE: Final = "generateContent"
COMPLETION_TIMEOUT_S: Final = 120.0
_MS: Final = 1000

logger = logging.getLogger(__name__)


class GeminiProvider(StructuredProvider):
    """The ``LlmProvider`` port over ``google-genai``."""

    kind: LlmProviderKind = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        reasoning_effort: ReasoningEffort | None = None,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(model, reasoning_effort)
        self.capabilities = LlmCapabilities(structured="json_schema", reasoning_effort=False)
        self._api_key = api_key
        self._transport = transport

    def _client(self, timeout_s: float) -> genai.Client:
        client_args: dict[str, Any] = {"follow_redirects": False}
        if self._transport is not None:
            client_args["transport"] = self._transport
        return genai.Client(
            api_key=self._api_key,
            http_options=types.HttpOptions(
                timeout=int(timeout_s * _MS),
                client_args=client_args,
                async_client_args=client_args,
            ),
        )

    async def list_models(self) -> list[str]:
        """Return the model ids that can generate content, without their prefix."""
        client = self._client(DEFAULT_TIMEOUT_S)
        found: set[str] = set()
        try:
            page = await client.aio.models.list()
            for model in page:
                actions = model.supported_actions
                if actions is not None and _GENERATE not in actions:
                    continue
                name = (model.name or "").removeprefix(_MODEL_PREFIX)
                if name:
                    found.add(name)
        except genai_errors.APIError as failure:
            raise problem_for(failure.code) from None
        except httpx.HTTPError:
            raise problem_for(None, connection=True) from None
        return sorted(found)

    async def complete(
        self, prompt: Prompt, schema: type[Any], retry_hint: str | None
    ) -> RawAnswer:
        """Ask for one object of ``schema`` and return the JSON Gemini produced."""
        config = types.GenerateContentConfig(
            system_instruction=messages_of(prompt.instructions, retry_hint),
            response_mime_type="application/json",
            response_schema=schema,
            temperature=prompt.temperature,
            max_output_tokens=self._max_output_tokens(prompt),
        )
        client = self._client(COMPLETION_TIMEOUT_S)
        try:
            response = await _generate(client, self._model, prompt.message, config)
        except genai_errors.APIError as failure:
            raise problem_for(failure.code) from None
        except httpx.HTTPError:
            raise problem_for(None, connection=True) from None
        return _answer_of(response)


async def _generate(
    client: genai.Client, model: str, message: str, config: types.GenerateContentConfig
) -> types.GenerateContentResponse:
    """Send one request. The SDK's ``contents`` union is too wide to narrow usefully."""
    return await client.aio.models.generate_content(  # pyright: ignore[reportUnknownMemberType]
        model=model, contents=message, config=config
    )


def _answer_of(response: types.GenerateContentResponse) -> RawAnswer:
    """Read the text and the token counts out of a Gemini answer."""
    text = response.text
    if not text:
        # A safety filter, or a cut-off: nothing to validate, nothing to repair.
        logger.warning("the AI provider answered with no text", extra={"llm": "gemini"})
        raise problems.llm_invalid_output()
    usage = response.usage_metadata
    return RawAnswer(
        text,
        LlmUsage(
            input_tokens=_count(usage, "prompt_token_count"),
            output_tokens=_count(usage, "candidates_token_count"),
        ),
    )


def _count(usage: object, name: str) -> int:
    value = cast("object", getattr(usage, name, None))
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
