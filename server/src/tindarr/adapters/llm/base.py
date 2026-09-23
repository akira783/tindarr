"""What every AI provider adapter has in common: the retry, the test, the bookkeeping.

An adapter only has to answer one question — "send this prompt and give me back the
text" — and this class turns that into the port: repair and validate the answer, retry
once on a schema failure with the *original* prompt, and report a connection test as a
coarse health value rather than as an exception.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Final

from pydantic import BaseModel

from tindarr.adapters.llm.structured import (
    RETRY_INSTRUCTION,
    InvalidOutputError,
    health_for,
    parse_structured,
)
from tindarr.core.errors import ProblemError
from tindarr.ports import problems
from tindarr.ports.connectors import ConnectionCheck
from tindarr.ports.llm import (
    Generation,
    LlmCapabilities,
    LlmProviderKind,
    LlmUsage,
    Prompt,
    ReasoningEffort,
)

#: How many times one prompt is sent. Two: the answer, and one correction. A third
#: attempt costs a third time and, in the fork's experience, changes nothing.
MAX_ATTEMPTS: Final = 2
#: What a provider that insists on a token budget is given when the caller named none.
DEFAULT_MAX_OUTPUT_TOKENS: Final = 8192

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RawAnswer:
    """One provider's answer, before anything has been made of it."""

    text: str
    usage: LlmUsage = field(default_factory=LlmUsage)


class StructuredProvider(ABC):
    """The ``LlmProvider`` port, minus the one call each provider makes differently."""

    kind: LlmProviderKind
    capabilities: LlmCapabilities

    def __init__(self, model: str, reasoning_effort: ReasoningEffort | None = None) -> None:
        self._model = model
        self._reasoning_effort = reasoning_effort

    @property
    def model(self) -> str:
        """The model id this provider was configured with."""
        return self._model

    @abstractmethod
    async def complete(
        self, prompt: Prompt, schema: type[BaseModel], retry_hint: str | None
    ) -> RawAnswer:
        """Send one prompt and return what came back, raising a mapped ``ProblemError``."""

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Return the model ids this provider offers."""

    async def test(self) -> ConnectionCheck:
        """Check the credentials by listing the models, without raising.

        Listing is the only call that proves a key works and costs nothing: a
        completion would be charged for, and an administrator presses "test" often.
        """
        try:
            models = await self.list_models()
        except ProblemError as problem:
            return ConnectionCheck(health_for(problem))
        if self._model and self._model not in models:
            logger.info(
                "the configured model is not in the provider's list",
                extra={"llm": self.kind},
            )
        return ConnectionCheck("ok", self.kind)

    async def generate[T: BaseModel](self, prompt: Prompt, schema: type[T]) -> Generation[T]:
        """Ask for one object of ``schema`` and return it validated.

        A schema failure is retried once, with the original prompt and one added
        instruction. The broken answer is never quoted back into the next prompt: it is
        untrusted text, and that is exactly the path an injection would take.
        """
        hint: str | None = None
        for attempt in range(MAX_ATTEMPTS):
            answer = await self.complete(prompt, schema, hint)
            try:
                value = parse_structured(answer.text, schema)
            except InvalidOutputError:
                hint = RETRY_INSTRUCTION
                continue
            return Generation(
                value=value, model=self._model, usage=answer.usage, retried=attempt > 0
            )
        logger.warning(
            "the AI provider never answered in the expected format",
            extra={"llm": self.kind, "attempts": MAX_ATTEMPTS},
        )
        raise problems.llm_invalid_output()

    def _max_output_tokens(self, prompt: Prompt) -> int:
        return prompt.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS
