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

**A call that failed is still a call.** An answer that never fits the schema was billed
twice and returns nothing; charging only the successes would make a strategy whose model
is broken look cheaper than one whose model works, on two metrics where lower is better.

**The tally is read, never trusted to stay put.** A strategy is handed the wrapped ports,
so it is handed a path to the counters; the harness therefore charges each batch the
*difference* between two snapshots and refuses a run whose totals went backwards
(``tindarr.swipe.evaluation.replay``). That is tamper evidence rather than a sandbox —
nothing in one process can stop code that is determined — but it turns "zero the meter"
from a silent win into a failed run.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from tindarr.core.errors import ProblemError
from tindarr.ports.connectors import ConnectionCheck
from tindarr.ports.llm import Generation, LlmCapabilities, LlmProvider, LlmProviderKind, Prompt
from tindarr.ports.metadata import (
    DiscoverQuery,
    ExternalMatch,
    Metadata,
    Provider,
    SearchQuery,
    Title,
    TitleDetails,
    TitleFilters,
    Trailer,
)
from tindarr.ports.titles import TitleRef

__all__ = ["BatchCost", "CostMeter", "CountingLlmProvider", "CountingMetadata"]

#: Characters per token, when a provider reports no usage at all. Crude on purpose: it
#: is labelled as an estimate everywhere it is printed.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class BatchCost:
    """What one batch spent: the difference between two readings of the meter."""

    metadata_calls: int = 0
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_calls_estimated: int = 0

    def since(self, earlier: "BatchCost") -> "BatchCost":
        """Return what was spent between ``earlier`` and this reading.

        Raises ``ValueError`` when a counter fell: counters only go up, so a smaller
        number means something wrote to the meter, and a cost nobody can stand behind is
        worse than no cost at all.
        """
        spent = BatchCost(
            metadata_calls=self.metadata_calls - earlier.metadata_calls,
            llm_calls=self.llm_calls - earlier.llm_calls,
            llm_input_tokens=self.llm_input_tokens - earlier.llm_input_tokens,
            llm_output_tokens=self.llm_output_tokens - earlier.llm_output_tokens,
            llm_calls_estimated=self.llm_calls_estimated - earlier.llm_calls_estimated,
        )
        if (
            min(
                spent.metadata_calls,
                spent.llm_calls,
                spent.llm_input_tokens,
                spent.llm_output_tokens,
                spent.llm_calls_estimated,
            )
            < 0
        ):
            msg = "the cost meter went backwards: something reset it during a batch"
            raise ValueError(msg)
        return spent

    def plus(self, other: "BatchCost") -> "BatchCost":
        """Return the two costs added together."""
        return BatchCost(
            metadata_calls=self.metadata_calls + other.metadata_calls,
            llm_calls=self.llm_calls + other.llm_calls,
            llm_input_tokens=self.llm_input_tokens + other.llm_input_tokens,
            llm_output_tokens=self.llm_output_tokens + other.llm_output_tokens,
            llm_calls_estimated=self.llm_calls_estimated + other.llm_calls_estimated,
        )

    @property
    def llm_tokens(self) -> int:
        """Every token, in and out."""
        return self.llm_input_tokens + self.llm_output_tokens


class CostMeter:
    """Everything one evaluation run spent, by port.

    The counters are read-only properties over a ledger only this class appends to, so
    the obvious way to make a strategy look cheap — ``metadata._meter.metadata_calls = 0``
    — raises instead of working. That is tamper *evidence*, not a sandbox: the harness
    runs a strategy in its own process and Python has no wall to put between them. What
    it does is make cheating deliberate rather than convenient, and the numbers that
    actually protect the gate are the ones a strategy cannot reach at all — whether its
    cards carry their details, and whether it beats the floors.
    """

    __slots__ = ("_llm", "_metadata")

    def __init__(self) -> None:
        self._metadata: list[str] = []
        self._llm: list[tuple[int, int, bool]] = []

    def __repr__(self) -> str:
        """Show the counters, for a failing test."""
        return (
            f"CostMeter(metadata_calls={self.metadata_calls}, llm_calls={self.llm_calls}, "
            f"llm_input_tokens={self.llm_input_tokens}, llm_output_tokens={self.llm_output_tokens})"
        )

    def record_metadata(self, operation: str) -> None:
        """Charge one metadata call."""
        self._metadata.append(operation)

    def record_llm(self, input_tokens: int, output_tokens: int, *, estimated: bool) -> None:
        """Charge one model call and its tokens, saying whether they were guessed."""
        self._llm.append((input_tokens, output_tokens, estimated))

    @property
    def metadata_calls(self) -> int:
        """How many metadata calls the run made."""
        return len(self._metadata)

    @property
    def metadata_operations(self) -> tuple[str, ...]:
        """One entry per metadata call, in order; the tests read it."""
        return tuple(self._metadata)

    @property
    def llm_calls(self) -> int:
        """How many generations the run asked for."""
        return len(self._llm)

    @property
    def llm_input_tokens(self) -> int:
        """Tokens sent."""
        return sum(row[0] for row in self._llm)

    @property
    def llm_output_tokens(self) -> int:
        """Tokens returned."""
        return sum(row[1] for row in self._llm)

    @property
    def llm_calls_measured(self) -> int:
        """Calls whose token counts the provider reported."""
        return sum(1 for row in self._llm if not row[2])

    @property
    def llm_calls_estimated(self) -> int:
        """Calls whose token counts had to be estimated from the prompt's length."""
        return sum(1 for row in self._llm if row[2])

    @property
    def llm_tokens(self) -> int:
        """Every token, in and out."""
        return self.llm_input_tokens + self.llm_output_tokens

    @property
    def tokens_fully_measured(self) -> bool:
        """Whether every LLM call reported its own token counts."""
        return self.llm_calls_estimated == 0

    def snapshot(self) -> BatchCost:
        """Read the counters, so a caller can charge a batch the difference."""
        return BatchCost(
            metadata_calls=self.metadata_calls,
            llm_calls=self.llm_calls,
            llm_input_tokens=self.llm_input_tokens,
            llm_output_tokens=self.llm_output_tokens,
            llm_calls_estimated=self.llm_calls_estimated,
        )


class CountingMetadata:
    """The ``Metadata`` port, plus a tally. Adds nothing else."""

    def __init__(self, inner: Metadata, meter: CostMeter) -> None:
        self._inner = inner
        self._meter = meter

    def _count(self, operation: str) -> None:
        self._meter.record_metadata(operation)

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

    async def discover(self, query: DiscoverQuery) -> list[Title]:
        """Read one discovery page, counted. A page is one request, whatever it holds."""
        self._count("discover")
        return await self._inner.discover(query)

    async def related(self, ref: TitleRef, language: str, page: int = 1) -> list[Title]:
        """Read one recommendations page, counted."""
        self._count("related")
        return await self._inner.related(ref, language, page)

    async def find_imdb(self, imdb_id: str, language: str) -> ExternalMatch | None:
        """Resolve an external id, counted. A replay never makes one; an import does."""
        self._count("find_imdb")
        return await self._inner.find_imdb(imdb_id, language)

    async def excluded_genre_ids(self, filters: TitleFilters) -> frozenset[int]:
        """Resolve the excluded genres, counted once however many pages it reads.

        The adapter caches TMDb's genre list for its own lifetime, so this is charged
        once per run in practice and never per card. Counting the operation rather than
        the requests underneath it slightly under-reports; it is two calls, once.
        """
        self._count("excluded_genre_ids")
        return await self._inner.excluded_genre_ids(filters)

    async def genres(self) -> Mapping[int, str]:
        """Read TMDb's genre list, counted once however many pages it really reads."""
        self._count("genres")
        return await self._inner.genres()

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
        """Ask for one object, and record what it cost — including when it fails.

        A generation that never validates was paid for twice (the adapter retries once)
        and returns nothing. Letting the exception carry the cost away with it would
        make a strategy whose model is broken read as *cheaper* than one whose model
        works, on two metrics where lower is better. So the failure is charged, from the
        prompt's own length, and marked estimated — the provider said nothing about what
        it billed, because it raised.
        """
        try:
            generation = await self._inner.generate(prompt, schema)
        except ProblemError:
            self._meter.record_llm(
                estimate_tokens((prompt.instructions, prompt.message)), 0, estimated=True
            )
            raise
        usage = generation.usage
        if usage.input_tokens or usage.output_tokens:
            self._meter.record_llm(usage.input_tokens, usage.output_tokens, estimated=False)
        else:
            self._meter.record_llm(
                estimate_tokens((prompt.instructions, prompt.message)),
                estimate_tokens((generation.value.model_dump_json(),)),
                estimated=True,
            )
        return generation


def estimate_tokens(texts: Sequence[str]) -> int:
    """Return a crude token count for text a provider charged for without saying so."""
    return sum(len(text) for text in texts) // CHARS_PER_TOKEN
