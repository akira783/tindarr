"""The candidate pool: TMDb retrieves, so the model no longer has to remember.

[ADR 0013](../../../../docs/adr/0013-recommendation-engine.md) measured the fork's
engine on 99 real votes and found the defect in its mechanism rather than in its
prompt. The model was asked for titles out of its own memory and TMDb was used to
*resolve* them, so it could only ever propose what is famous enough to be memorised: 47
% of the cards were titles the user had already seen, and a batch of fifteen
suggestions yielded about six usable cards.

This module is the other way round. TMDb builds the pool — what people who liked the
same things went on to watch, plus filtered discovery — and the model's job shrinks to
choosing from it and saying why. Three things follow:

- **A title that does not exist cannot be proposed.** Every candidate came back from
  TMDb with an id, so nothing has to be searched for afterwards and nothing is dropped
  for failing to resolve.
- **Last week's releases are in reach**, which no model's memory is.
- **Every exclusion is applied to the pool, before the model sees it.** Voted on,
  already served, already in the library, outside the household's content filters,
  the wrong media type: all of it is gone from what is handed over. Filtering the
  *answer* instead would mean paying for cards that are then thrown away, and it would
  rest on a model honouring a "never propose" list — which the fork's numbers show it
  does only approximately.

**The adaptive popularity floor**, and the ceiling ADR 0013 does not name. The floor is
the ADR's: the bolder the novelty setting, the lower the popularity a candidate is
allowed to have. On its own it widens the pool downwards without narrowing it at the
top, and the already-seen cards ADR 0013 counted were overwhelmingly *famous* — so the
band here also has a **fame ceiling**, expressed in TMDb vote counts rather than in
popularity. Popularity is a rolling measure of this week's activity; the vote count is
how many people ever had an opinion, which is much closer to "they have probably already
seen it". Whether the ceiling earns its place is a question for the harness, not for
this docstring: both halves of the band are constants named below, and
``docs/evaluation.md`` carries the numbers they produced.

Nothing here reads a clock, a database or a random source. Two calls with the same
context produce the same pool, which is what lets a replay be compared with another.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from tindarr.core.errors import ProblemError
from tindarr.ports.metadata import DiscoverOrder, DiscoverQuery, Metadata, Title, TitleFilters
from tindarr.ports.titles import MediaKind, TitleRef
from tindarr.swipe.strategy import Novelty, PickKind, StrategyContext

__all__ = [
    "NOVELTY_BANDS",
    "POOL_SIZE",
    "Band",
    "CandidatePool",
    "PoolSource",
    "Retrieval",
]

logger = logging.getLogger(__name__)

#: How many candidates a batch is chosen from. Large enough that the model has a real
#: choice, small enough that the pool is a few thousand tokens rather than a document:
#: ADR 0013's whole argument is yield per call, and a pool nobody reads is paid for.
POOL_SIZE: Final = 60
#: How many liked titles are used as recommendation seeds. The most recent ones, because
#: a taste moves and the last thing somebody liked says more than the first.
MAX_SEEDS: Final = 6
#: A seed's recommendations past this rank are the long tail of one title's page and add
#: little the next seed does not add better.
SEED_DEPTH: Final = 12


@dataclass(frozen=True, slots=True)
class Band:
    """What a novelty level admits into the pool.

    ``popularity_floor`` is ADR 0013's adaptive floor, in TMDb's own popularity units:
    the bolder the setting, the lower it sits. ``max_votes`` is the fame ceiling that
    the ADR's already-seen numbers argue for and that it does not itself specify.
    """

    #: ADR 0013's adaptive floor. Drops as the novelty setting rises.
    popularity_floor: float
    #: How many TMDb votes a candidate needs before it is worth a card at all. Below
    #: this a title is usually a listing rather than a film anybody can watch.
    min_votes: int
    #: The fame ceiling, or ``None`` for "no ceiling". A title with more votes than this
    #: is one most people have heard of.
    max_votes: int | None
    #: Which pages of the discovery sort to read. Page one of "most popular" *is* the
    #: wall of blockbusters; reaching past it is most of what novelty means.
    pages: tuple[int, ...]
    #: How much of the pool comes from "people who liked this also liked" rather than
    #: from discovery. The comfort end of the dial leans on what is already proven.
    seeded_share: float


#: The three levels the fork shipped, with numbers attached for the first time. The fork
#: expressed novelty as a sentence in the prompt and nothing else, so a "bold" deck was
#: bold only if the model felt like it; here it is a filter TMDb applies.
NOVELTY_BANDS: Final[Mapping[Novelty, Band]] = {
    "familiar": Band(
        popularity_floor=5.0,
        min_votes=600,
        max_votes=None,
        pages=(1, 2),
        seeded_share=0.7,
    ),
    "balanced": Band(
        popularity_floor=2.0,
        min_votes=150,
        max_votes=None,
        pages=(1, 2, 3),
        seeded_share=0.5,
    ),
    "bold": Band(
        popularity_floor=0.0,
        min_votes=40,
        max_votes=4000,
        pages=(2, 3, 4),
        seeded_share=0.35,
    ),
}


@dataclass(frozen=True, slots=True)
class CandidatePool:
    """What a strategy may choose from, and where each candidate came from.

    Already filtered: nothing in ``titles`` is voted on, served, owned, of the wrong
    media type or outside the household's content filters. A strategy that hands one of
    these straight back has not skipped a check, because the check happened here.
    """

    titles: tuple[Title, ...] = ()
    #: ``safe`` for a candidate that came from something the user liked, ``explore``
    #: for one discovery found. The hybrid strategy uses it as the default pick kind,
    #: and the model may still say otherwise about its own choice.
    origin: Mapping[TitleRef, PickKind] = field(default_factory=dict[TitleRef, PickKind])
    #: TMDb's genre list, ``id -> name``. A listing returns ids; a candidate offered to
    #: a model without its genres is a title string and a year, which is nothing to
    #: match a taste against.
    genres: Mapping[int, str] = field(default_factory=dict[int, str])

    def genre_names(self, title: Title) -> tuple[str, ...]:
        """Return the genres of one candidate, in TMDb's own words and order."""
        found = (self.genres.get(genre) for genre in title.genre_ids)
        return tuple(name for name in found if name is not None)

    def __len__(self) -> int:
        """How many candidates the pool holds."""
        return len(self.titles)

    @property
    def by_ref(self) -> Mapping[TitleRef, Title]:
        """The pool, addressed by title. What "the model chose one of ours" checks."""
        return {title.ref: title for title in self.titles}

    @property
    def seeded(self) -> int:
        """How many candidates came from a title the user liked."""
        return sum(1 for kind in self.origin.values() if kind == "safe")


class PoolSource(Protocol):
    """Where a strategy gets its candidates. ``Retrieval`` is the one implementation.

    A port rather than the class itself, for the same reason every other boundary here
    is one: a strategy's tests must be able to hand it a pool without a TMDb behind it,
    and the retrieval layer's tests must be able to drive it without a strategy.
    """

    async def pool(self, context: StrategyContext, *, calibration: bool = False) -> CandidatePool:
        """Return the candidates this user could be shown next, best first."""
        ...


class Retrieval:
    """Builds a candidate pool from TMDb for one user's run.

    One instance per user, like a strategy: the cache below holds only TMDb's public
    answers, but an object shared between two people is an object that can carry one
    person's state into another's batch, and the replay's guarantees are not worth
    weakening for a handful of saved calls.
    """

    def __init__(self, metadata: Metadata, size: int = POOL_SIZE) -> None:
        self._metadata = metadata
        self._size = size
        #: Answers already paid for in this user's run. A deck asks for the same
        #: discovery pages batch after batch, and a household is not billed twice for
        #: the same page because the exclusions moved.
        self._discovered: dict[tuple[str, ...], tuple[Title, ...]] = {}
        self._excluded_genres: frozenset[int] | None = None
        self._names: dict[int, str] | None = None

    async def pool(self, context: StrategyContext, *, calibration: bool = False) -> CandidatePool:
        """Return the candidates this user could be shown next, best first.

        The order is deterministic and is the one a strategy that does no thinking of
        its own should follow: the recommendations of the titles they liked most
        recently, interleaved with filtered discovery in the proportion the novelty
        level asks for.

        ``calibration`` asks the opposite question of the pool. A calibration batch is
        trying to find out what somebody has *already* watched, so it wants the famous
        titles every other batch is trying to avoid: the familiar band, and the most
        voted-on titles rather than the ones popular this week.
        """
        band = NOVELTY_BANDS["familiar" if calibration else context.novelty]
        order: DiscoverOrder = "votes" if calibration else "popularity"
        excluded_genres = await self._genre_ids(context.filters)
        seeded = await self._seeded(context)
        found = await self._discovery(context, band, excluded_genres, order)
        keep = _Keeper(context, band, excluded_genres)
        ranked = _interleave(keep(seeded), keep(found), band.seeded_share, self._size)
        if not ranked:
            logger.info(
                "the candidate pool came back empty",
                extra={"novelty": context.novelty, "batch": context.batch_index},
            )
        origin: dict[TitleRef, PickKind] = {}
        for title, pick in ranked:
            origin.setdefault(title.ref, pick)
        return CandidatePool(
            titles=tuple(title for title, _ in ranked),
            origin=origin,
            genres=await self._genre_names(),
        )

    async def _genre_names(self) -> Mapping[int, str]:
        if self._names is None:
            try:
                self._names = dict(await self._metadata.genres())
            except ProblemError:
                # A pool without its genre names is a worse pool, not a failed batch.
                logger.info("TMDb did not answer with its genre list")
                self._names = {}
        return self._names

    # --- the two sources --------------------------------------------------------------

    async def _seeded(self, context: StrategyContext) -> list[Title]:
        """Return what people who liked this user's favourites went on to watch.

        The seeds are the titles they reacted well to, most recent first. A seed TMDb
        cannot answer for is skipped: one dead id must not cost a batch, and the whole
        point of retrieval is that the deck survives a bad answer.
        """
        wanted = _kinds(context.media_kind)
        seeds = [ref for ref in reversed(context.liked) if ref.kind in wanted][:MAX_SEEDS]
        found: list[Title] = []
        for seed in seeds:
            page = await self._related(seed, context.language)
            found.extend(page[:SEED_DEPTH])
        return found

    async def _discovery(
        self,
        context: StrategyContext,
        band: Band,
        excluded_genres: frozenset[int],
        order: DiscoverOrder,
    ) -> list[Title]:
        """Read the filtered discovery pages TMDb answers once the band is applied."""
        found: list[Title] = []
        for kind in sorted(_kinds(context.media_kind)):
            for page in band.pages:
                found.extend(
                    await self._discover(
                        DiscoverQuery(
                            kind=kind,
                            language=context.language,
                            page=page,
                            order=order,
                            min_votes=band.min_votes,
                            max_votes=band.max_votes,
                            from_year=context.filters.min_year,
                            without_genres=tuple(sorted(excluded_genres)),
                            include_adult=not context.filters.exclude_adult,
                        )
                    )
                )
        return found

    # --- the cache and the failures ---------------------------------------------------

    async def _related(self, ref: TitleRef, language: str) -> tuple[Title, ...]:
        key = ("related", ref.kind, str(ref.tmdb_id), language)
        cached = self._discovered.get(key)
        if cached is not None:
            return cached
        try:
            found = tuple(await self._metadata.related(ref, language))
        except ProblemError:
            # One seed TMDb will not answer for. The other seeds and the discovery
            # pages still make a pool, and a deck that fails because a single id has
            # been deleted upstream is a deck nobody can rely on.
            logger.info("TMDb had no recommendations for a liked title", extra={"kind": ref.kind})
            found = ()
        self._discovered[key] = found
        return found

    async def _discover(self, query: DiscoverQuery) -> tuple[Title, ...]:
        key = _discover_key(query)
        cached = self._discovered.get(key)
        if cached is not None:
            return cached
        try:
            found = tuple(await self._metadata.discover(query))
        except ProblemError:
            logger.info("TMDb did not answer a discovery page", extra={"kind": query.kind})
            found = ()
        self._discovered[key] = found
        return found

    async def _genre_ids(self, filters: TitleFilters) -> frozenset[int]:
        if self._excluded_genres is None:
            try:
                self._excluded_genres = await self._metadata.excluded_genre_ids(filters)
            except ProblemError:
                # A filter that cannot be resolved must not become a filter that is not
                # applied: the names are still checked against each candidate's genres
                # by the household's own list, and only the server-side hint is lost.
                logger.info("TMDb did not answer with its genre list")
                self._excluded_genres = frozenset()
        return self._excluded_genres


def _kinds(wanted: MediaKind | None) -> frozenset[MediaKind]:
    """Which media types this context admits."""
    return frozenset({wanted}) if wanted is not None else frozenset({"movie", "tv"})


def _discover_key(query: DiscoverQuery) -> tuple[str, ...]:
    return (
        "discover",
        query.kind,
        query.language,
        str(query.page),
        query.order,
        str(query.min_votes),
        str(query.max_votes),
        str(query.min_rating),
        str(query.from_year),
        str(query.to_year),
        ",".join(str(genre) for genre in sorted(query.with_genres)),
        ",".join(str(genre) for genre in sorted(query.without_genres)),
        str(query.original_language),
        str(query.origin_country),
        str(query.include_adult),
    )


class _Keeper:
    """Every exclusion, applied to the pool rather than to the model's answer."""

    __slots__ = ("_band", "_excluded", "_genres", "_kinds", "_languages", "_min_year", "_no_adult")

    def __init__(
        self, context: StrategyContext, band: Band, excluded_genres: frozenset[int]
    ) -> None:
        self._excluded = context.excluded
        self._kinds = _kinds(context.media_kind)
        self._band = band
        self._genres = excluded_genres
        filters = context.filters
        self._no_adult = filters.exclude_adult
        self._min_year = filters.min_year
        self._languages = {value.casefold() for value in filters.excluded_original_languages}

    def __call__(self, titles: Iterable[Title]) -> list[Title]:
        """Return the candidates that survive, in the order they arrived, once each."""
        seen: set[TitleRef] = set()
        kept: list[Title] = []
        for title in titles:
            if title.ref in seen or not self._keeps(title):
                continue
            seen.add(title.ref)
            kept.append(title)
        return kept

    def _keeps(self, title: Title) -> bool:
        if title.ref in self._excluded or title.ref.kind not in self._kinds:
            return False
        if self._no_adult and title.adult:
            return False
        if self._min_year is not None and title.year is not None and title.year < self._min_year:
            return False
        language = (title.original_language or "").casefold()
        if language and language in self._languages:
            return False
        if self._genres and self._genres.intersection(title.genre_ids):
            return False
        return self._in_band(title)

    def _in_band(self, title: Title) -> bool:
        """Apply the adaptive popularity floor, the quality floor and the fame ceiling.

        A recommendations page honours none of them — the endpoint takes no filters — so
        they are applied here to both sources, and a candidate that came in through a
        liked title is held to the same band as one that came in through discovery.
        """
        band = self._band
        if title.popularity < band.popularity_floor:
            return False
        if title.vote_count < band.min_votes:
            return False
        return band.max_votes is None or title.vote_count <= band.max_votes


def _interleave(
    seeded: Sequence[Title], found: Sequence[Title], share: float, size: int
) -> list[tuple[Title, PickKind]]:
    """Merge the two sources in the asked-for proportion, without repeating a title.

    Deterministic, and it degrades in both directions: a user with no likes yet gets a
    pool of pure discovery, and a discovery page TMDb refused costs recommendations
    nothing. Whichever source runs out first, the other fills the rest — a pool short of
    candidates is a batch short of cards.
    """
    wanted_seeded = round(size * share)
    taken: set[TitleRef] = set()
    ranked: list[tuple[Title, PickKind]] = []
    seeded_left = list(seeded)
    found_left = list(found)
    while len(ranked) < size and (seeded_left or found_left):
        # Which source is behind its quota decides the next card, so the proportion
        # holds at every prefix and not only at the end: a batch of ten taken off the
        # front of the pool has the mix the novelty setting asked for.
        so_far = sum(1 for _, pick in ranked if pick == "safe")
        from_seeds = bool(seeded_left) and so_far * size < wanted_seeded * (len(ranked) + 1)
        source = seeded_left if (from_seeds or not found_left) else found_left
        pick: PickKind = "safe" if source is seeded_left else "explore"
        title = source.pop(0)
        if title.ref in taken:
            continue
        taken.add(title.ref)
        ranked.append((title, pick))
    return ranked
