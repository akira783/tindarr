"""The calibration grid: a wall of posters, ticked in three minutes, worth fifteen years.

[ADR 0013](../../../docs/adr/0013-recommendation-engine.md) measured the already-seen
problem and then measured every obvious cure. Importing Netflix, Jellyfin and Seerr
together would have avoided **3 of the 47** already-seen cards, because the cards people
have already seen are famous films and series from the 1990s to the 2010s and nobody's
media server knows about a film watched in a cinema fifteen years ago. The distribution
is the whole argument: 1 from the 1980s, 8 from the 1990s, 6 from the 2000s, 22 from the
2010s, 10 from the 2020s.

So the grid asks. It is the cheapest possible interface — posters, one tap each, no
opinion required — and it is aimed exactly where the misses are.

**What makes a good grid title**, and it is not "the most popular thing on TMDb". A
poster is worth showing when the answer is genuinely uncertain: *Inception* is on
everybody's wall and tells you almost nothing, and a film with four hundred votes tells
you nothing either because nobody has seen it. What carries information is a title that
*a lot* of people have an opinion about, spread over the decades and the kinds of film
somebody's taste actually differs across. Hence four rules:

- **Ranked by vote count, not popularity.** Popularity is a rolling measure of this
  week's activity; the vote count is how many people ever had an opinion, which is what
  "you have probably seen this" means. It is the same choice the novelty band makes.
- **Spread across eras.** One slice per decade, interleaved, so a wall is not thirty
  films from the 2010s. The ADR's own distribution is the specification.
- **Spread across languages and genres.** A slice of what is famous *in the user's own
  language* sits beside the internationally famous, because a French household's
  already-seen list is not an American one's; and no single genre may take more than a
  third of the wall.
- **Never a title we already know the answer to.** Everything voted on, owned, imported
  or ticked on an earlier grid is gone before the wall is built — including the posters
  the user answered "no" to, which is why "no" is worth storing.

**A tick is not a vote.** It writes a ``WatchedTitle`` (``tindarr.ports.history``), the
retrieval layer excludes it, the stats never count it and no strategy replays it. The
person said they have seen a film; they did not say anything about a card.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from tindarr.core.errors import ProblemError
from tindarr.ports.history import WatchedTitle
from tindarr.ports.metadata import DiscoverQuery, Metadata, Title, TitleFilters
from tindarr.ports.titles import MediaKind, TitleRef
from tindarr.swipe.retrieval import passes_filters

__all__ = [
    "ERAS",
    "GRID_MIN_VOTES",
    "GRID_SIZE",
    "MAX_GRID_PAGE",
    "MAX_GRID_SIZE",
    "CalibrationGrid",
    "GridRequest",
    "GridTick",
    "GridTitle",
    "grid_history",
]

logger = logging.getLogger(__name__)

#: How many posters a wall holds by default. Enough to be worth the trip, few enough to
#: answer in about three minutes at four seconds a poster.
GRID_SIZE: Final = 40
#: The most a client may ask for in one page, so nobody turns the grid into a scraper.
MAX_GRID_SIZE: Final = 60
#: How deep the grid pages. Past this, the wall has stopped being famous titles.
MAX_GRID_PAGE: Final = 10
#: How many people must have voted on a title before it is worth asking about. Below
#: this, "have you seen it?" is answered "no" by almost everybody and teaches nothing.
GRID_MIN_VOTES: Final = 1_500
#: One slice per decade, oldest last so the wall opens on what most people have seen.
#: The windows are ADR 0013's own distribution of the already-seen cards.
ERAS: Final[tuple[tuple[int, int | None], ...]] = (
    (2020, None),
    (2010, 2019),
    (2000, 2009),
    (1990, 1999),
    (1970, 1989),
)
#: No genre may take more than this share of one wall. Without it, the most-voted films
#: of every decade are an action wall, and an action wall asks one question five times.
_MAX_GENRE_SHARE: Final = 1 / 3
#: Two passes over the decades: what is famous everywhere, then what is famous here.
_AXES: Final[tuple[bool, ...]] = (False, True)
#: English says nothing about where somebody lives, so an English-speaking household's
#: local slice is chosen by country instead.
_UNINFORMATIVE_LANGUAGE: Final = "en"
#: ISO 3166-1 alpha-2.
_COUNTRY_LENGTH: Final = 2


@dataclass(frozen=True, slots=True)
class GridTitle:
    """One poster on the wall."""

    ref: TitleRef
    title: str
    year: int | None = None
    poster_path: str | None = None
    #: How many people ever voted on it. Shown nowhere; it is why this one is here.
    vote_count: int = 0


@dataclass(frozen=True, slots=True)
class GridTick:
    """One answer: this title, and whether the person has watched it."""

    ref: TitleRef
    seen: bool


@dataclass(frozen=True, slots=True)
class GridRequest:
    """What one wall is built from. The whole input, as everywhere in this package."""

    language: str = "en"
    region: str = "US"
    #: What the household refuses to be shown. A grid honours them: asking somebody
    #: about a genre they filtered out is asking a question nobody wanted.
    filters: TitleFilters = field(default_factory=TitleFilters)
    #: Everything the answer is already known for: voted on, owned, imported, or ticked
    #: on an earlier wall — "no, never seen it" included.
    known: frozenset[TitleRef] = frozenset()
    media_kind: MediaKind | None = None
    page: int = 1
    size: int = GRID_SIZE


class CalibrationGrid:
    """Builds a wall of famous posters for one household, from TMDb discovery.

    One instance per request is fine: the only state is a cache of the pages this wall
    read, which exists so that two kinds sharing a slice are not two requests.
    """

    def __init__(self, metadata: Metadata) -> None:
        self._metadata = metadata
        self._pages: dict[tuple[str, ...], tuple[Title, ...]] = {}

    async def build(self, request: GridRequest) -> tuple[GridTitle, ...]:
        """Return the posters to show, best first, already filtered and spread.

        Costs one TMDb request per era and kind, plus the same again for the household's
        own language — at most twenty for a wall of forty, and a wall is built a handful
        of times in an account's life.
        """
        excluded_genres = await self._excluded_genres(request.filters)
        # Decade-minor, so the round-robin below hands out one decade per poster and a
        # wall answered five posters deep has already asked about five decades.
        slices = [
            await self._slice(request, era, kind, local=local)
            for local in _AXES
            for kind in sorted(_kinds(request.media_kind))
            for era in ERAS
        ]
        keep = _Keeper(request, excluded_genres)
        wall = _interleave([keep(found) for found in slices], request.size)
        if not wall:
            logger.info("the calibration grid came back empty", extra={"page": request.page})
        return tuple(
            GridTitle(
                ref=found.ref,
                title=found.title,
                year=found.year,
                poster_path=found.poster_path,
                vote_count=found.vote_count,
            )
            for found in wall
        )

    async def _excluded_genres(self, filters: TitleFilters) -> frozenset[int]:
        """Resolve the household's excluded genres, failing closed as the pool does."""
        if not filters.excluded_genres:
            return frozenset()
        return await self._metadata.excluded_genre_ids(filters)

    async def _slice(
        self, request: GridRequest, era: tuple[int, int | None], kind: MediaKind, *, local: bool
    ) -> tuple[Title, ...]:
        query = DiscoverQuery(
            kind=kind,
            language=request.language,
            page=min(max(request.page, 1), MAX_GRID_PAGE),
            order="votes",
            min_votes=GRID_MIN_VOTES,
            from_year=era[0],
            to_year=era[1],
            original_language=_local_language(request.language) if local else None,
            origin_country=_local_country(request.language, request.region) if local else None,
            include_adult=False,
        )
        key = (
            "grid",
            kind,
            str(query.page),
            str(era[0]),
            str(era[1]),
            str(query.original_language),
            str(query.origin_country),
            query.language,
        )
        cached = self._pages.get(key)
        if cached is not None:
            return cached
        try:
            found = tuple(await self._metadata.discover(query))
        except ProblemError:
            # One slice TMDb refused costs one decade of the wall, not the wall.
            logger.info("TMDb did not answer a calibration slice", extra={"kind": kind})
            found = ()
        self._pages[key] = found
        return found


def grid_history(ticks: Iterable[GridTick], *, now: datetime) -> tuple[WatchedTitle, ...]:
    """Turn the answers to a wall into history rows.

    A tick says "I have watched this" and nothing about how much of it, so the row
    carries no ``state``: it excludes the title from the deck and it is deliberately not
    an engagement, because inventing "finished" out of a tap is inventing a taste signal.
    A "no" is stored too, with ``seen`` false, so the next wall asks something else.
    """
    # Last answer wins: a wall the user scrolled back up in sends the same ref twice.
    latest = {tick.ref: tick for tick in ticks}
    return tuple(
        WatchedTitle(ref=ref, source="grid", seen=tick.seen) for ref, tick in latest.items()
    )


def _local_language(language: str) -> str | None:
    """Return the original language of the local slice, or ``None`` when it says nothing."""
    base = (language or "").split("-")[0].casefold()
    return None if not base or base == _UNINFORMATIVE_LANGUAGE else base


def _local_country(language: str, region: str) -> str | None:
    """Return the country of the local slice, for a language that says nothing."""
    if _local_language(language) is not None:
        return None
    country = (region or "").strip().upper()
    return country if len(country) == _COUNTRY_LENGTH and country.isalpha() else None


def _kinds(wanted: MediaKind | None) -> frozenset[MediaKind]:
    return frozenset({wanted}) if wanted is not None else frozenset({"movie", "tv"})


class _Keeper:
    """Everything a wall must not ask about, applied before the wall is built."""

    __slots__ = ("_excluded", "_filters", "_genres", "_kinds", "_seen")

    def __init__(self, request: GridRequest, excluded_genres: frozenset[int]) -> None:
        self._excluded = request.known
        self._kinds = _kinds(request.media_kind)
        self._filters = request.filters
        self._genres = excluded_genres
        self._seen: set[TitleRef] = set()

    def __call__(self, titles: Iterable[Title]) -> list[Title]:
        """Return the posters worth showing, in the order they arrived, once each."""
        kept: list[Title] = []
        for found in titles:
            if found.ref in self._excluded or found.ref in self._seen:
                continue
            if found.ref.kind not in self._kinds or found.vote_count < GRID_MIN_VOTES:
                continue
            if not passes_filters(found, self._filters, self._genres):
                continue
            self._seen.add(found.ref)
            kept.append(found)
        return kept


def _interleave(slices: Sequence[Sequence[Title]], size: int) -> list[Title]:
    """Take one poster from each slice in turn, preferring an under-represented genre.

    Round-robin rather than concatenation, so the wall alternates decades and languages
    from its very first row: somebody who ticks ten posters and stops has still answered
    about five decades.

    The genre cap is a **preference inside a slice**, not a veto. Within one decade's
    answers it reaches past the third action film to the drama underneath, which is what
    stops a wall asking one question five times; it never skips a decade to honour it,
    because a wall that repeats a decade has lost more than a wall that repeats a genre.
    """
    cap = max(int(size * _MAX_GENRE_SHARE), 1)
    per_genre: dict[int, int] = {}
    wall: list[Title] = []
    queues = [list(found) for found in slices]
    while len(wall) < size and any(queues):
        for queue in queues:
            if len(wall) >= size:
                break
            found = _take(queue, per_genre, cap)
            if found is None:
                continue
            genre = _first_genre(found)
            if genre is not None:
                per_genre[genre] = per_genre.get(genre, 0) + 1
            wall.append(found)
    return wall


def _take(queue: list[Title], per_genre: Mapping[int, int], cap: int) -> Title | None:
    """Take this slice's next poster, skipping a genre the wall already has enough of."""
    for position, found in enumerate(queue):
        genre = _first_genre(found)
        if genre is None or per_genre.get(genre, 0) < cap:
            return queue.pop(position)
    return queue.pop(0) if queue else None


def _first_genre(found: Title) -> int | None:
    """TMDb lists a title's genres most-defining first; that one is what a wall counts."""
    return found.genre_ids[0] if found.genre_ids else None
