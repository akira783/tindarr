"""OpenAI, Mistral and every OpenAI-compatible endpoint, through the official SDK.

Three of the contract's six provider kinds are this one adapter. Mistral publishes an
OpenAI-compatible API and is a preset — a base URL and nothing else — and
``openai_compatible`` is the same thing with the address typed in, which covers
OpenRouter, Groq, LM Studio, vLLM and local proxies.

That breadth is why the request is built as a **ladder** rather than as one shape. A
gateway that advertises the OpenAI API rarely implements all of it, and the way it says
so is a ``400`` naming the parameter it did not like. So the strictest request is sent
first — a real JSON schema, plus the reasoning effort when one is configured — and each
refusal of that kind drops one rung: reasoning effort, then the schema for plain JSON
mode, then nothing but the instruction to answer in JSON. Only a refusal that names the
parameter counts; everything else is the provider's own answer and is reported.

``max_completion_tokens`` is sent rather than ``max_tokens``: the reasoning models
reject the older name outright, and every current gateway accepts the newer one.
"""

import logging
from collections.abc import Iterator
from http import HTTPStatus
from typing import Any, Final, cast

import httpx2
import openai

from tindarr.adapters.http import DEFAULT_TIMEOUT_S
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

#: OpenAI's own endpoint, written out rather than left to the SDK's default: the
#: console shows the address a connector really calls, and "whatever the library thinks
#: today" is not something to show somebody.
OPENAI_BASE_URL: Final = "https://api.openai.com/v1"
#: Mistral's OpenAI-compatible endpoint. The preset is a base URL and nothing more.
MISTRAL_BASE_URL: Final = "https://api.mistral.ai/v1"
#: Words a gateway uses when it refuses one of the optional parameters.
_REJECTED: Final = (
    "response_format",
    "json_schema",
    "json_object",
    "reasoning_effort",
    "unsupported_parameter",
    "unsupported_value",
)
#: How long one completion may take. Far above a normal answer, below a hung socket.
COMPLETION_TIMEOUT_S: Final = 120.0

logger = logging.getLogger(__name__)


def is_parameter_rejection(failure: openai.APIStatusError) -> bool:
    """Whether a ``400`` is the endpoint refusing a parameter rather than the request."""
    if failure.status_code != HTTPStatus.BAD_REQUEST:
        return False
    text = f"{failure} {getattr(failure, 'message', '')} {failure.body}".casefold()
    return any(word in text for word in _REJECTED)


class OpenAiProvider(StructuredProvider):
    """The ``LlmProvider`` port over the OpenAI SDK."""

    def __init__(  # noqa: PLR0913 - one argument per stored connector field
        self,
        api_key: str,
        model: str,
        *,
        kind: LlmProviderKind = "openai",
        base_url: str = OPENAI_BASE_URL,
        reasoning_effort: ReasoningEffort | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(model, reasoning_effort)
        self.kind: LlmProviderKind = kind
        # Only the provider's own endpoint is promised a real JSON schema; a gateway
        # says what it can do by refusing, which the ladder below handles.
        self.capabilities = LlmCapabilities(
            structured="json_schema", reasoning_effort=kind == "openai"
        )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    @property
    def base_url(self) -> str:
        """The address this provider really calls.

        The ``openai`` kind reaches OpenAI whatever a base URL left behind by an earlier
        provider says, which is what the factory enforces and a test checks.
        """
        return self._base_url

    def _client(self, timeout_s: float) -> openai.AsyncOpenAI:
        return openai.AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            # Retries are the caller's business: a batch that is already late must not
            # silently take three times as long, and a quota error is not retryable.
            max_retries=0,
            http_client=httpx2.AsyncClient(
                timeout=timeout_s, follow_redirects=False, transport=self._transport
            ),
        )

    async def list_models(self) -> list[str]:
        """Return the model ids the endpoint offers, so none is ever hard-coded."""
        client = self._client(DEFAULT_TIMEOUT_S)
        try:
            page = await client.models.list()
        except openai.APIStatusError as failure:
            raise problem_for(failure.status_code) from None
        except openai.APIError:
            raise problem_for(None, connection=True) from None
        finally:
            await client.close()
        return model_ids(model.id for model in page.data)

    async def complete(
        self, prompt: Prompt, schema: type[Any], retry_hint: str | None
    ) -> RawAnswer:
        """Send one prompt, dropping the parameters this endpoint refuses."""
        client = self._client(COMPLETION_TIMEOUT_S)
        ladder = list(self._ladder(prompt, schema, retry_hint))
        try:
            for index, body in enumerate(ladder):
                try:
                    return _answer_of(await _create(client, body))
                except openai.APIStatusError as failure:
                    if index + 1 < len(ladder) and is_parameter_rejection(failure):
                        logger.info(
                            "the AI endpoint refused a parameter; trying a simpler request",
                            extra={"llm": self.kind, "rung": index},
                        )
                        continue
                    raise problem_for(failure.status_code) from None
                except openai.APIError:
                    raise problem_for(None, connection=True) from None
        finally:
            await client.close()
        raise problems.llm_unreachable()  # pragma: no cover - the ladder always ends

    def _ladder(
        self, prompt: Prompt, schema: type[Any], retry_hint: str | None
    ) -> Iterator[dict[str, Any]]:
        base: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": messages_of(prompt.instructions, retry_hint)},
                {"role": "user", "content": prompt.message},
            ],
            "max_completion_tokens": self._max_output_tokens(prompt),
        }
        if prompt.temperature is not None:
            base["temperature"] = prompt.temperature
        strict: dict[str, Any] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": json_schema_of(schema),
                "strict": True,
            },
        }
        if self._reasoning_effort is not None and self.capabilities.reasoning_effort:
            yield base | {"response_format": strict, "reasoning_effort": self._reasoning_effort}
        yield base | {"response_format": strict}
        yield base | {"response_format": {"type": "json_object"}}
        yield base


async def _create(client: openai.AsyncOpenAI, body: dict[str, Any]) -> object:
    """Send one chat completion.

    The request is built as a mapping — the ladder adds and removes parameters — so the
    SDK's overloads cannot type it. This is the one place that is true, and its result
    is read back attribute by attribute in ``_answer_of``.
    """
    return cast("object", await client.chat.completions.create(**body))  # pyright: ignore[reportUnknownMemberType]


def _answer_of(response: object) -> RawAnswer:
    """Read the text and the token counts out of a chat completion."""
    choices = getattr(response, "choices", None)
    text: str | None = None
    if isinstance(choices, list) and choices:
        message = getattr(choices[0], "message", None)  # pyright: ignore[reportUnknownArgumentType]
        content = getattr(message, "content", None)
        text = content if isinstance(content, str) else None
    if text is None:
        # A provider that answers with no content at all (a filter, a cut-off): there is
        # nothing to validate and nothing to repair.
        raise problems.llm_invalid_output()
    return RawAnswer(text, _usage_of(getattr(response, "usage", None)))


def _usage_of(usage: object) -> LlmUsage:
    return LlmUsage(
        input_tokens=_count(usage, "prompt_tokens", "input_tokens"),
        output_tokens=_count(usage, "completion_tokens", "output_tokens"),
    )


def _count(usage: object, *names: str) -> int:
    for name in names:
        value = getattr(usage, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0
