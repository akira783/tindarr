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

**And the floor turned out not to be the lever.** It is applied to the pool, so every
candidate has already passed it — the harness reports ``above_floor`` at 100 % for every
strategy on both vote sets, which means no strategy can ever be measured against it.
What the popularity profile did find is that the hybrid's picks sit *above their own
pool's vote-count median* in eight batches of nine, while both floors sit below theirs:
the drift is in the ranking, in vote counts, and at the top of the distribution rather
than the bottom. ``Band.famous_share`` is the budget that answers it, spent by the
strategy against ``CandidatePool.median_votes`` rather than filtered out here — a
candidate the band admits is a candidate somebody may legitimately be shown, and taking
it out of the pool would take it away from every batch instead of from this one.

Nothing here reads a clock, a database or a random source. Two calls with the same
context produce the same pool, which is what lets a replay be compared with another.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from tindarr.core.errors import ProblemError
from tindarr.ports.metadata import (
    DiscoverOrder,
    DiscoverQuery,
    Metadata,
    Title,
    TitleDetails,
    TitleFilters,
)
from tindarr.ports.titles import MediaKind, TitleRef
from tindarr.swipe.strategy import Novelty, PickKind, StrategyContext

__all__ = [
    "NOVELTY_BANDS",
    "POOL_SIZE",
    "Band",
    "CandidatePool",
    "PoolSource",
    "Retrieval",
    "card_details",
    "passes_filters",
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
    #: How much of one **batch** may be drawn from the most-rated quarter of the pool.
    #:
    #: ADR 0013's floor is a floor on ``popularity`` and it is applied to the pool, so
    #: every card has already passed it and no strategy can be measured against it: the
    #: harness reports ``above_floor`` at 100 % for all six committed runs. The fault the
    #: ADR is about sits on the other axis and at the other end — the already-seen cards
    #: were the *most-voted* titles, and the hybrid's picks were above their own pool's
    #: vote-count median in eight batches of nine. This is the budget that stops that,
    #: on the axis the measurement found, and it adapts with the novelty setting the way
    #: the ADR asks the floor to. ``1.0`` is "no budget", which is what the comfort end
    #: and every calibration batch want.
    famous_share: float = 1.0


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
        famous_share=1.0,
    ),
    "balanced": Band(
        popularity_floor=2.0,
        min_votes=150,
        max_votes=None,
        pages=(1, 2, 3),
        seeded_share=0.5,
        famous_share=0.2,
    ),
    "bold": Band(
        popularity_floor=0.0,
        min_votes=40,
        max_votes=4000,
        pages=(2, 3, 4),
        seeded_share=0.35,
        famous_share=0.1,
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
    #: The novelty band this pool was built under: ADR 0013's adaptive popularity floor
    #: and the fame window every candidate here already passed. Carried on the pool
    #: rather than recomputed by whoever needs it, because two places deciding what
    #: "balanced" means is how a filter and a prompt start disagreeing. ``None`` for a
    #: pool built by hand, which is what a strategy's own tests hand it.
    band: Band | None = None

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

    @property
    def famous_votes(self) -> int:
        """The vote count that marks off the most-rated quarter of this pool.

        The threshold the fame budget is spent against, and the one the prompt quotes.
        It is a property of the **pool** rather than a constant, because what "famous"
        means depends on what TMDb answered for this band on this day: a number written
        into the code would be a budget that tightens and loosens on its own.

        The upper quartile rather than the median, because the fault is at the tail and
        not in the middle. The titles the author had already seen were rated by 12 000 to
        33 000 people; the ones they liked and had not seen, by around 5 000. A budget
        spent against the median pushes the whole deck down and takes the second group
        with it, which is what the first attempt measured: fewer already-seen cards, and
        the confirmable likes gone with them.
        """
        ordered = sorted(title.vote_count for title in self.titles)
        return ordered[int(0.75 * (len(ordered) - 1) + 0.5)] if ordered else 0

    def famous(self, size: int) -> int:
        """How many of ``size`` cards may come from the most-rated quarter of the pool."""
        share = self.band.famous_share if self.band is not None else 1.0
        return size if share >= 1.0 else round(size * share)


class PoolSource(Protocol):
    """Where a strategy gets its candidates. ``Retrieval`` is the one implementation.

    A port rather than the class itself, for the same reason every other boundary here
    is one: a strategy's tests must be able to hand it a pool without a TMDb behind it,
    and the retrieval layer's tests must be able to drive it without a strategy.
    """

    async def pool(self, context: StrategyContext) -> CandidatePool:
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

    async def pool(self, context: StrategyContext) -> CandidatePool:
        """Return the candidates this user could be shown next, best first.

        The order is deterministic and is the one a strategy that does no thinking of
        its own should follow: the recommendations of the titles they liked most
        recently, interleaved with filtered discovery in the proportion the novelty
        level asks for.

        A **calibration** batch asks the opposite question of the pool, and the context
        decides it, not the caller: a calibration batch is trying to find out what
        somebody has *already* watched, so it wants the famous titles every other batch
        avoids — the familiar band, sorted by vote count rather than by what is popular
        this week. Deciding it here is what keeps the floors and the candidate on one
        shortlist; when the strategy decided, the floors quietly got a different pool
        for every calibration batch and the comparison was not one.
        """
        calibrating = context.calibrating
        band = NOVELTY_BANDS["familiar" if calibrating else context.novelty]
        order: DiscoverOrder = "votes" if calibrating else "popularity"
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
            band=band,
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
        """Resolve the household's excluded genres, or refuse to build a pool.

        **This one fails closed**, unlike every other TMDb call here. A candidate only
        carries genre *ids*, so a household that excluded a genre by name has no filter
        at all until TMDb's list has been read: returning an empty set would not be "the
        filter is a little coarser today", it would be the filter switched off, on every
        card, silently. A household that asked not to be shown horror gets an error and
        an empty deck instead, and the failure is not cached, so the next batch tries
        again.
        """
        if not filters.excluded_genres:
            return frozenset()
        if self._excluded_genres is None:
            self._excluded_genres = await self._metadata.excluded_genre_ids(filters)
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


async def card_details(metadata: Metadata, ref: TitleRef, language: str) -> TitleDetails | None:
    """Read one card's details, or ``None`` when TMDb will not answer for it.

    Nine good cards beat a batch that died on the tenth. A title TMDb has just deleted,
    or a request that timed out, costs that card — the harness's ``complete_rate``
    counts what came back with its details, so the loss is visible rather than fatal.
    """
    try:
        return await metadata.details(ref, language)
    except ProblemError as failure:
        logger.info(
            "TMDb would not describe a chosen card; it is dropped from the batch",
            extra={"kind": ref.kind, "reason": failure.code},
        )
        return None


def passes_filters(title: Title, filters: TitleFilters, excluded_genres: frozenset[int]) -> bool:
    """Whether a candidate survives the household's content filters.

    The one implementation the domain uses — the baselines call it too, so the floors
    and the candidate cannot be handed different shortlists the day somebody sets
    ``min_year``. It is deliberately identical to ``tindarr.adapters.tmdb.passes``,
    which the adapter applies when it resolves a search, and a test holds the two to
    each other. A missing year or language passes, as it does there: refusing what TMDb
    simply did not fill in would quietly empty a deck.
    """
    if filters.exclude_adult and title.adult:
        return False
    if filters.min_year is not None and title.year is not None and title.year < filters.min_year:
        return False
    language = (title.original_language or "").casefold()
    if language and language in {value.casefold() for value in filters.excluded_original_languages}:
        return False
    return not (excluded_genres and excluded_genres.intersection(title.genre_ids))


class _Keeper:
    """Every exclusion, applied to the pool rather than to the model's answer."""

    __slots__ = ("_band", "_excluded", "_filters", "_genres", "_kinds")

    def __init__(
        self, context: StrategyContext, band: Band, excluded_genres: frozenset[int]
    ) -> None:
        self._excluded = context.excluded
        self._kinds = _kinds(context.media_kind)
        self._band = band
        self._genres = excluded_genres
        self._filters = context.filters

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
        if not passes_filters(title, self._filters, self._genres):
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
