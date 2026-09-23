"""Anthropic, through the official SDK and a forced tool call.

Anthropic has no "answer in this JSON schema" parameter. What it has is tools, and a
tool call *is* a schema-constrained object: the schema goes in as the tool's
``input_schema``, ``tool_choice`` forces that one tool, and the model's arguments come
back already structured. That is the shape ADR 0005 chose, and it is stricter than any
"please answer in JSON" instruction could be.

Anthropic's current API has **no temperature**, so ``Prompt.temperature`` is ignored
here and ``capabilities.temperature`` says so. Variety is asked for in the prompt on
this provider, which is what its own guidance recommends anyway.

The object is re-serialised to text before it leaves this module. Not because text is
wanted, but because there is then one validation path for six providers: whatever a
provider does natively, the shared layer repairs, parses and validates exactly the same
way, and a schema failure retries exactly the same way.
"""

import json
import logging
from typing import Any, Final, cast

import anthropic
import httpx2

from tindarr.adapters.http import DEFAULT_TIMEOUT_S
from tindarr.adapters.llm.base import RawAnswer, StructuredProvider
from tindarr.adapters.llm.structured import json_schema_of, messages_of, problem_for
from tindarr.ports import problems
from tindarr.ports.llm import (
    LlmCapabilities,
    LlmProviderKind,
    LlmUsage,
    Prompt,
    ReasoningEffort,
)

#: The name of the one tool every call offers. It is never shown to anyone.
TOOL_NAME: Final = "answer"
TOOL_DESCRIPTION: Final = "Return the answer as this object. Call this tool and nothing else."
#: Models fetched per page when listing. Anthropic's catalogue is far smaller.
_MODEL_PAGE: Final = 100
COMPLETION_TIMEOUT_S: Final = 120.0

logger = logging.getLogger(__name__)


class AnthropicProvider(StructuredProvider):
    """The ``LlmProvider`` port over the Anthropic SDK."""

    kind: LlmProviderKind = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        reasoning_effort: ReasoningEffort | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(model, reasoning_effort)
        self.capabilities = LlmCapabilities(
            structured="json_schema", reasoning_effort=False, temperature=False
        )
        self._api_key = api_key
        self._transport = transport

    def _client(self, timeout_s: float) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(
            api_key=self._api_key,
            max_retries=0,
            http_client=httpx2.AsyncClient(
                timeout=timeout_s, follow_redirects=False, transport=self._transport
            ),
        )

    async def list_models(self) -> list[str]:
        """Return the model ids the account may use."""
        client = self._client(DEFAULT_TIMEOUT_S)
        try:
            page = await client.models.list(limit=_MODEL_PAGE)
        except anthropic.APIStatusError as failure:
            raise problem_for(failure.status_code) from None
        except anthropic.APIError:
            raise problem_for(None, connection=True) from None
        finally:
            await client.close()
        return sorted({model.id for model in page.data if model.id})

    async def complete(
        self, prompt: Prompt, schema: type[Any], retry_hint: str | None
    ) -> RawAnswer:
        """Ask for one tool call carrying the schema, and return its arguments as text."""
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_output_tokens(prompt),
            "system": messages_of(prompt.instructions, retry_hint),
            "messages": [{"role": "user", "content": prompt.message}],
            "tools": [
                {
                    "name": TOOL_NAME,
                    "description": TOOL_DESCRIPTION,
                    "input_schema": json_schema_of(schema),
                }
            ],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
        }
        client = self._client(COMPLETION_TIMEOUT_S)
        try:
            response = await _create(client, body)
        except anthropic.APIStatusError as failure:
            raise problem_for(failure.status_code) from None
        except anthropic.APIError:
            raise problem_for(None, connection=True) from None
        finally:
            await client.close()
        return _answer_of(response)


async def _create(client: anthropic.AsyncAnthropic, body: dict[str, Any]) -> object:
    """Send one message. The request is built as a mapping, so it is untyped here."""
    return cast("object", await client.messages.create(**body))  # pyright: ignore[reportUnknownMemberType]


def _answer_of(response: object) -> RawAnswer:
    """Find the tool call in the answer and serialise its arguments."""
    blocks = getattr(response, "content", None)
    arguments: object = None
    if isinstance(blocks, list):
        for block in cast("list[object]", blocks):
            if getattr(block, "type", None) == "tool_use":
                arguments = getattr(block, "input", None)
                break
    if arguments is None:
        # The model answered with prose, or with nothing: there is no object to validate.
        logger.warning("the AI provider did not call the answer tool", extra={"llm": "anthropic"})
        raise problems.llm_invalid_output()
    usage = getattr(response, "usage", None)
    return RawAnswer(
        json.dumps(arguments, ensure_ascii=False, default=str),
        LlmUsage(
            input_tokens=_count(usage, "input_tokens"),
            output_tokens=_count(usage, "output_tokens"),
        ),
    )


def _count(usage: object, name: str) -> int:
    value = getattr(usage, name, None)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
