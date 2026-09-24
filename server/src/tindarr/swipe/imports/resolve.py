"""Turning a line of somebody's file into a TMDb id, or refusing to.

This is the part every comparable project gets wrong, and it gets it wrong in one line:
``results[0]``. A viewing history says *Away*; TMDb's first answer for "Away" is a film
nobody in the household has watched; the import writes it down as watched and the deck
silently stops offering a series the user might have loved. There is no error message
and no way to find out afterwards, because the wrong answer looks exactly like a right
one.

So the rule here is **abstain rather than guess**. Every candidate is scored, the best
one is kept only when it is good enough, and everything else goes into a review queue
where a person decides in one click. An import that matched forty titles and asks about
six is a better import than one that matched forty-six, and it is the only one whose
numbers can be trusted afterwards.

**How a candidate is scored.** Similarity first, popularity second, in that order and
never mixed into a single number: a 0.62 match on a famous film must never outrank a
0.94 match on an obscure one, which is exactly what multiplying them does. Similarity is
``difflib``'s ratio over both the translated and the original title, on the normalised
spelling (``tindarr.swipe.imports.netflix.normalized``, where ``&`` becomes ``et``).
Popularity only ever breaks a tie between two candidates on the same side of the
confidence line — which is where a real ambiguity lives: two series with the same name,
one of which everybody has heard of.

**Three spellings are tried, not one.** The row as written, the part before a French
`` : `` subtitle, and the row with its colons flattened to spaces. A search that comes
back with an exact match stops the rest, the other media type included: TMDb is free but
it is not ours.

**The household's content filters are deliberately not applied here.** A filter says what
the deck may *offer*, and it is applied to the candidate pool where that decision lives.
What somebody watched is a fact about them, and a household that stopped wanting horror
did not stop having seen it — dropping those rows would lose the engagement signal and
leave the titles in the pool, which is exactly backwards.

**An IMDb id is not searched for at all.** It is resolved through ``/find``, exactly.
An id that identifies a title is not a string that resembles one, and the whole reason
ADR 0013 keeps ratings exports is that they carry both an id *and* an opinion.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Final

from tindarr.core.errors import ProblemError
from tindarr.ports.metadata import Metadata, SearchQuery, Title
from tindarr.ports.titles import MediaKind, TitleRef
from tindarr.swipe.imports.netflix import normalized
from tindarr.swipe.imports.records import WatchedItem

__all__ = [
    "CONFIDENT_SIMILARITY",
    "MAX_ALTERNATIVES",
    "Candidate",
    "Resolution",
    "TitleResolver",
]

logger = logging.getLogger(__name__)

#: How alike two titles must be before a match is written down without asking. Measured
#: on a real five-month Netflix export: at 0.85, every wrong match in that file lands in
#: the review queue and every right one goes straight through.
CONFIDENT_SIMILARITY: Final = 0.85
#: A match this close is the answer; the remaining spellings are not searched for.
_EXACT_ENOUGH: Final = 0.99
#: How deep into one search's answers a candidate can be. Past ten, TMDb is listing
#: things that share a word.
_SEARCH_DEPTH: Final = 10
#: How many other candidates a review entry offers. Enough to recognise the right one,
#: few enough to read at a glance.
MAX_ALTERNATIVES: Final = 4
#: Consecutive TMDb failures before the run is abandoned. One dead search costs one
#: title; five in a row means the service is down and the rest of the file would be
#: review entries about nothing.
_MAX_CONSECUTIVE_FAILURES: Final = 5


@dataclass(frozen=True, slots=True)
class Candidate:
    """One title TMDb offered for a row, with how well it answers it."""

    ref: TitleRef
    title: str
    year: int | None = None
    poster_path: str | None = None
    #: 0 to 1, over the normalised spellings.
    similarity: float = 0.0
    popularity: float = 0.0

    @property
    def confident(self) -> bool:
        """Whether this is close enough to write down without asking anybody."""
        return self.similarity >= CONFIDENT_SIMILARITY

    @property
    def rank(self) -> tuple[bool, float, float]:
        """The sort key: confident first, then similarity, then fame as a tie-break."""
        return (self.confident, round(self.similarity, 3), self.popularity)


@dataclass(frozen=True, slots=True)
class Resolution:
    """What one row of a file turned out to be, or why it could not be settled."""

    item: WatchedItem
    #: The best candidate, or ``None`` when TMDb offered nothing at all.
    best: Candidate | None = None
    #: Other candidates worth showing in the review queue, best first.
    alternatives: tuple[Candidate, ...] = ()
    #: ``True`` when the id came from an external id rather than from a search, in
    #: which case there is nothing to be confident about: it is the title or nothing.
    exact: bool = False
    #: ``True`` when the id named an episode, so it counts as one episode of a series.
    episode: bool = False

    @property
    def confident(self) -> bool:
        """Whether this resolution may be written without asking the user."""
        return self.best is not None and (self.exact or self.best.confident)


class MetadataDownError(RuntimeError):
    """TMDb stopped answering; the rest of this file would be noise."""


class TitleResolver:
    """Resolves one user's import against TMDb, caching what it already asked.

    One instance per import, like a strategy is one per user: the cache holds only
    TMDb's public answers, but an object shared between two imports is an object that
    can carry one person's file into another's results.
    """

    def __init__(self, metadata: Metadata, language: str) -> None:
        self._metadata = metadata
        self._language = language
        self._searches: dict[tuple[str, str], tuple[Title, ...]] = {}
        self._failures = 0

    async def resolve(self, item: WatchedItem) -> Resolution:
        """Identify one row, or hand it to the review queue.

        Raises ``MetadataDownError`` once TMDb has refused several times in a row.
        """
        if item.imdb_id is not None:
            return await self._by_id(item)
        return await self._by_search(item)

    # --- the exact path -----------------------------------------------------------

    async def _by_id(self, item: WatchedItem) -> Resolution:
        imdb_id = item.imdb_id or ""
        try:
            found = await self._metadata.find_imdb(imdb_id, self._language)
        except ProblemError:
            self._fail()
            logger.info("TMDb would not resolve an imported id")
            return Resolution(item=item)
        self._failures = 0
        if found is None:
            # An id TMDb has never heard of is not an ambiguity: there is nothing for a
            # person to choose between, so it is reported as unmatched and not queued.
            return Resolution(item=item)
        candidate = Candidate(
            ref=found.ref,
            title=found.title,
            year=found.year,
            poster_path=found.poster_path,
            similarity=1.0,
        )
        return Resolution(item=item, best=candidate, exact=True, episode=found.episode)

    # --- the search path ----------------------------------------------------------

    async def _by_search(self, item: WatchedItem) -> Resolution:
        wanted = normalized(item.query)
        if not wanted:
            return Resolution(item=item)
        found: list[Candidate] = []
        for kind in _kinds(item.kind_hint):
            for spelling in _spellings(item.query):
                found.extend(await self._candidates(kind, spelling, wanted, item.year))
                if _settled(found):
                    break
            if _settled(found):
                # An exact answer settles the row: the other media type and the other
                # spellings would only cost requests to confirm it.
                break
        if not found:
            return Resolution(item=item)
        ranked = _best_per_title(sorted(found, key=lambda entry: entry.rank, reverse=True))
        return Resolution(
            item=item, best=ranked[0], alternatives=tuple(ranked[1 : 1 + MAX_ALTERNATIVES])
        )

    async def _candidates(
        self, kind: MediaKind, spelling: str, wanted: str, year: int | None
    ) -> list[Candidate]:
        return [
            Candidate(
                ref=title.ref,
                title=title.title,
                year=title.year,
                poster_path=title.poster_path,
                similarity=_similarity(wanted, title),
                popularity=title.popularity,
            )
            for title in await self._search(kind, spelling, year)
        ]

    async def _search(self, kind: MediaKind, spelling: str, year: int | None) -> tuple[Title, ...]:
        key = (kind, spelling)
        cached = self._searches.get(key)
        if cached is not None:
            return cached
        query = SearchQuery(
            title=spelling,
            kind=kind,
            year=year,
            language=self._language,
            include_adult=False,
        )
        try:
            found = tuple((await self._metadata.search(query))[:_SEARCH_DEPTH])
        except ProblemError:
            self._fail()
            logger.info("TMDb would not answer a search for an imported title")
            return ()
        self._failures = 0
        self._searches[key] = found
        return found

    def _fail(self) -> None:
        self._failures += 1
        if self._failures >= _MAX_CONSECUTIVE_FAILURES:
            raise MetadataDownError


def _settled(found: Sequence[Candidate]) -> bool:
    """Whether one of the candidates is close enough to stop looking."""
    return any(candidate.similarity >= _EXACT_ENOUGH for candidate in found)


def _kinds(hint: MediaKind | None) -> tuple[MediaKind, ...]:
    """Which way to search, and in which order. The file's own guess goes first."""
    if hint == "tv":
        return ("tv", "movie")
    return ("movie", "tv")


def _spellings(query: str) -> tuple[str, ...]:
    """Return the forms of one row worth searching for, in order, without repeats.

    The row as written; the part before a French `` : `` subtitle, which is how
    distributors write *The Witcher : Les sirènes des abysses*; and the row with its
    colons flattened, for a catalogue that punctuates it differently.

    The last two are computed on a copy whose no-break spaces are ordinary ones. French
    typography writes the space before a colon as U+00A0 or U+202F, so a plain
    ``split(" : ")`` silently never fires on the rows that need it most: on the author's
    real export it is the difference between *The Handmaid\u2019s Tale : La Servante
    écarlate* resolving and resolving to nothing at all.
    """
    plain = query.replace("\u00a0", " ").replace("\u202f", " ")
    forms = (query, plain.split(" : ", maxsplit=1)[0], plain.replace(":", " "))
    return tuple(dict.fromkeys(form.strip() for form in forms if form.strip()))


def _similarity(wanted: str, title: Title) -> float:
    """How alike a row and a candidate are, over both spellings TMDb carries."""
    names = (title.title, title.original_title)
    return max(
        (SequenceMatcher(None, wanted, normalized(name)).ratio() for name in names if name),
        default=0.0,
    )


def _best_per_title(ranked: Sequence[Candidate]) -> list[Candidate]:
    """Drop the repeats a second spelling brings back, keeping each title's best score."""
    seen: set[TitleRef] = set()
    kept: list[Candidate] = []
    for candidate in ranked:
        if candidate.ref in seen:
            continue
        seen.add(candidate.ref)
        kept.append(candidate)
    return kept
