"""What a batch cost, counted at the port rather than guessed from the code.

A strategy that picks better by asking the model three times and TMDb forty times has
not necessarily picked better. ADR 0013's whole argument is about yield per call, so the
harness wraps the two ports a strategy may reach and counts what goes through them. The
wrappers are transparent: they add no behaviour, so a strategy cannot tell it is being
measured and a measured run is the same run.

**Tokens are measured when the provider says so and estimated otherwise.** Ollama and
some OpenAI-compatible endpoints report nothing; rather than print a zero that reads
like "free", the harness estimates from the prompt's own length and says, in the output,
how many calls it had to estimate.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from tindarr.ports.connectors import ConnectionCheck
from tindarr.ports.llm import Generation, LlmCapabilities, LlmProvider, LlmProviderKind, Prompt
from tindarr.ports.metadata import Metadata, Provider, SearchQuery, Title, TitleDetails, Trailer
from tindarr.ports.titles import TitleRef

__all__ = ["CostMeter", "CountingLlmProvider", "CountingMetadata"]

#: Characters per token, when a provider reports no usage at all. Crude on purpose: it
#: is labelled as an estimate everywhere it is printed.
CHARS_PER_TOKEN = 4


@dataclass(slots=True)
class CostMeter:
    """Everything one evaluation run spent, by port."""

    metadata_calls: int = 0
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    #: Calls whose token counts the provider reported.
    llm_calls_measured: int = 0
    #: Calls whose token counts had to be estimated from the prompt's length.
    llm_calls_estimated: int = 0
    #: One entry per metadata call, in order; the tests read it, the report counts it.
    metadata_operations: list[str] = field(default_factory=list[str])

    @property
    def llm_tokens(self) -> int:
        """Every token, in and out."""
        return self.llm_input_tokens + self.llm_output_tokens

    @property
    def tokens_fully_measured(self) -> bool:
        """Whether every LLM call reported its own token counts."""
        return self.llm_calls_estimated == 0


class CountingMetadata:
    """The ``Metadata`` port, plus a tally. Adds nothing else."""

    def __init__(self, inner: Metadata, meter: CostMeter) -> None:
        self._inner = inner
        self._meter = meter

    def _count(self, operation: str) -> None:
        self._meter.metadata_calls += 1
        self._meter.metadata_operations.append(operation)

    async def test(self) -> ConnectionCheck:
        """Check the key, counted like any other call."""
        self._count("test")
        return await self._inner.test()

    async def search(self, query: SearchQuery) -> list[Title]:
        """Search, counted."""
        self._count("search")
        return await self._inner.search(query)

    async def match(self, query: SearchQuery) -> Title | None:
        """Match, counted once for the operation; its own searches count too."""
        self._count("match")
        return await self._inner.match(query)

    async def details(self, ref: TitleRef, language: str) -> TitleDetails:
        """Read a title, counted."""
        self._count("details")
        return await self._inner.details(ref, language)

    async def watch_providers(self, ref: TitleRef, region: str) -> list[Provider]:
        """Read where a title streams, counted."""
        self._count("watch_providers")
        return await self._inner.watch_providers(ref, region)

    async def trailer(self, ref: TitleRef, language: str) -> Trailer | None:
        """Read a trailer, counted."""
        self._count("trailer")
        return await self._inner.trailer(ref, language)

    async def region_providers(self, region: str) -> list[Provider]:
        """Read a region's providers, counted."""
        self._count("region_providers")
        return await self._inner.region_providers(region)


class CountingLlmProvider:
    """The ``LlmProvider`` port, plus a tally of calls and tokens."""

    def __init__(self, inner: LlmProvider, meter: CostMeter) -> None:
        self._inner = inner
        self._meter = meter
        self.kind: LlmProviderKind = inner.kind
        self.capabilities: LlmCapabilities = inner.capabilities

    async def test(self) -> ConnectionCheck:
        """Check the credentials; not counted, it buys no cards."""
        return await self._inner.test()

    async def list_models(self) -> list[str]:
        """List the models; not counted, it buys no cards."""
        return await self._inner.list_models()

    async def generate[T: BaseModel](self, prompt: Prompt, schema: type[T]) -> Generation[T]:
        """Ask for one object, and record what it cost."""
        generation = await self._inner.generate(prompt, schema)
        meter = self._meter
        meter.llm_calls += 1
        usage = generation.usage
        if usage.input_tokens or usage.output_tokens:
            meter.llm_calls_measured += 1
            meter.llm_input_tokens += usage.input_tokens
            meter.llm_output_tokens += usage.output_tokens
        else:
            meter.llm_calls_estimated += 1
            meter.llm_input_tokens += estimate_tokens((prompt.instructions, prompt.message))
            meter.llm_output_tokens += estimate_tokens((generation.value.model_dump_json(),))
        return generation


def estimate_tokens(texts: Sequence[str]) -> int:
    """Return a crude token count for text a provider charged for without saying so."""
    return sum(len(text) for text in texts) // CHARS_PER_TOKEN
