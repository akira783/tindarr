"""Helpers for the swipe strategy and evaluation tests.

``InMemoryMetadata`` implements the ``Metadata`` port straight from a list of titles,
which is what a strategy under test needs; the harness's own tests drive the **real**
TMDb adapter over a recorded cassette instead, so both halves of the chain are covered.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from tindarr.core.errors import ProblemError
from tindarr.ports.connectors import ConnectionCheck
from tindarr.ports.llm import (
    Generation,
    LlmCapabilities,
    LlmProviderKind,
    LlmUsage,
    Prompt,
)
from tindarr.ports.metadata import (
    DiscoverQuery,
    Provider,
    SearchQuery,
    Title,
    TitleDetails,
    TitleFilters,
    Trailer,
)
from tindarr.ports.titles import MediaKind, TitleRef
from tindarr.swipe.retrieval import CandidatePool
from tindarr.swipe.strategy import Candidate, StrategyContext


def title(  # noqa: PLR0913 - a title is a bag of fields; naming them beats a builder
    tmdb_id: int,
    name: str,
    *,
    kind: MediaKind = "movie",
    popularity: float = 1.0,
    year: int = 2016,
    language: str = "en",
    adult: bool = False,
    genre_ids: tuple[int, ...] = (878,),
) -> Title:
    """A search-result title, with only the fields a strategy reads."""
    return Title(
        ref=TitleRef(kind, tmdb_id),
        title=name,
        year=year,
        popularity=popularity,
        original_language=language,
        adult=adult,
        genre_ids=genre_ids,
    )


@dataclass
class InMemoryMetadata:
    """TMDb reduced to a dictionary, counting what was asked of it."""

    titles: Sequence[Title] = field(default_factory=tuple[Title, ...])
    calls: list[str] = field(default_factory=list[str])
    #: What ``discover`` answers for one ``(kind, page)``; the whole list otherwise.
    pages: Mapping[tuple[MediaKind, int], Sequence[Title]] = field(
        default_factory=dict[tuple[MediaKind, int], Sequence[Title]]
    )
    #: What ``related`` answers for one seed. Empty means "TMDb knows nothing".
    recommendations: Mapping[TitleRef, Sequence[Title]] = field(
        default_factory=dict[TitleRef, Sequence[Title]]
    )
    #: TMDb's genre list, as ``id -> name``.
    genre_names: Mapping[int, str] = field(default_factory=lambda: {878: "Science Fiction"})

    async def test(self) -> ConnectionCheck:
        self.calls.append("test")
        return ConnectionCheck(health="ok")

    async def search(self, query: SearchQuery) -> list[Title]:
        self.calls.append(f"search:{query.title}")
        wanted = query.title.casefold()
        return [row for row in self.titles if row.title.casefold() == wanted]

    async def match(self, query: SearchQuery) -> Title | None:
        return next(iter(await self.search(query)), None)

    async def discover(self, query: DiscoverQuery) -> list[Title]:
        self.calls.append(f"discover:{query.kind}:{query.page}")
        page = self.pages.get((query.kind, query.page))
        if page is not None:
            return list(page)
        return [row for row in self.titles if row.ref.kind == query.kind]

    async def related(self, ref: TitleRef, language: str, page: int = 1) -> list[Title]:
        self.calls.append(f"related:{ref.kind}:{ref.tmdb_id}:{page}")
        return list(self.recommendations.get(ref, ()))

    async def genres(self) -> Mapping[int, str]:
        self.calls.append("genre-list")
        return self.genre_names

    async def excluded_genre_ids(self, filters: TitleFilters) -> frozenset[int]:
        # Nothing to resolve costs nothing, as in the real adapter.
        if not filters.excluded_genres:
            return frozenset()
        self.calls.append("genres")
        return frozenset(int(name) for name in filters.excluded_genres if name.isdigit())

    async def details(self, ref: TitleRef, language: str) -> TitleDetails:
        self.calls.append(f"details:{ref.kind}:{ref.tmdb_id}:{language}")
        found = next((row for row in self.titles if row.ref == ref), None)
        name = found.title if found is not None else f"{ref.kind} {ref.tmdb_id}"
        return TitleDetails(ref=ref, title=name, year=found.year if found else None)

    async def watch_providers(self, ref: TitleRef, region: str) -> list[Provider]:
        self.calls.append(f"providers:{ref.tmdb_id}:{region}")
        return []

    async def trailer(self, ref: TitleRef, language: str) -> Trailer | None:
        self.calls.append(f"trailer:{ref.tmdb_id}:{language}")
        return None

    async def region_providers(self, region: str) -> list[Provider]:
        self.calls.append(f"region:{region}")
        return []


@dataclass
class ScriptedStrategy:
    """Proposes exactly what it was told to, batch by batch, and records what it saw.

    The workhorse of the replay tests: it makes the scoring rules easy to state (this
    card is a duplicate, that one is owned) and it keeps every ``StrategyContext`` it
    was handed, which is how the tests check that no vote from the future ever reached
    one.
    """

    batches: Sequence[Sequence[TitleRef]] = field(default_factory=tuple[tuple[TitleRef, ...], ...])
    name: str = "scripted"
    seen: list[StrategyContext] = field(default_factory=list[StrategyContext])

    async def propose(self, context: StrategyContext, size: int) -> Sequence[Candidate]:
        self.seen.append(context)
        index = context.batch_index
        refs = self.batches[index] if index < len(self.batches) else ()
        return [Candidate(ref=ref) for ref in refs]


@dataclass
class RecordedEngine:
    """The engine that produced the votes, replayed: it proposes them in their own order.

    A scoring oracle, and **only** a test double: it is handed the whole vote history,
    including the part the replay is withholding, which no real strategy may see. That
    is the point. Feeding the recorded cards back through the harness must reproduce the
    numbers the recording was measured at — 47 % already seen, 63 % liked among the new
    (ADR 0013) — and anything else means the replay is mis-scoring.
    """

    votes: Mapping[str, Sequence[TitleRef]]
    name: str = "recorded"

    async def propose(self, context: StrategyContext, size: int) -> Sequence[Candidate]:
        order = self.votes.get(context.user_id, ())
        excluded = context.excluded
        upcoming = [ref for ref in order if ref not in excluded]
        return [Candidate(ref=ref) for ref in upcoming[:size]]


@dataclass
class FixedPool:
    """A ``PoolSource`` that hands back the same candidates, already filtered or not.

    The retrieval layer has its own tests. A strategy's tests are about what it does
    with a pool, so they get one straight, without a band, a page or a TMDb behind it.
    """

    titles: Sequence[Title] = field(default_factory=tuple[Title, ...])
    #: Which candidates count as "came from something they liked".
    seeded: frozenset[TitleRef] = field(default_factory=frozenset[TitleRef])
    calls: list[bool] = field(default_factory=list[bool])

    async def pool(self, context: StrategyContext, *, calibration: bool = False) -> CandidatePool:
        self.calls.append(calibration)
        # The real one filters; this one does too, so a test that forgets an exclusion
        # does not pass here and fail in production.
        kept = [title for title in self.titles if title.ref not in context.excluded]
        return CandidatePool(
            titles=tuple(kept),
            origin={
                title.ref: ("safe" if title.ref in self.seeded else "explore") for title in kept
            },
        )


@dataclass
class ScriptedLlm:
    """An ``LlmProvider`` that answers with prepared JSON, or refuses.

    The provider adapters have their own contract tests against the real SDKs; what a
    strategy's tests need is control over *what came back*, including the answers a real
    provider only produces on a bad day.
    """

    answers: list[str] = field(default_factory=list[str])
    #: Raised instead of answering, once per entry, before the answers are used.
    fails: list[ProblemError] = field(default_factory=list[ProblemError])
    prompts: list[Prompt] = field(default_factory=list[Prompt])
    kind: LlmProviderKind = "openai_compatible"
    capabilities: LlmCapabilities = field(default_factory=LlmCapabilities)

    async def test(self) -> ConnectionCheck:
        return ConnectionCheck(health="ok")

    async def list_models(self) -> list[str]:
        return ["model-a"]

    async def generate[T: BaseModel](self, prompt: Prompt, schema: type[T]) -> Generation[T]:
        self.prompts.append(prompt)
        if self.fails:
            raise self.fails.pop(0)
        text = self.answers.pop(0) if self.answers else "{}"
        return Generation(
            value=schema.model_validate_json(text),
            model="model-a",
            usage=LlmUsage(input_tokens=len(prompt.message) // 4, output_tokens=len(text) // 4),
        )
