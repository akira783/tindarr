"""Two strategies that are deliberately stupid, so the harness has a floor to beat.

Neither is meant to ship. They exist because a metric with nothing to compare it to is
decoration: "63 % of the new cards were liked" only means something next to what picking
the most popular unseen title, or picking at random, would have scored on the same
votes. They are also the proof that the port of ``tindarr.swipe.strategy`` can be
implemented at all without a database, a clock or an HTTP framework.

Both read their candidates from a pool handed to them at construction. Building that
pool from TMDb — similarity, discovery, the adaptive popularity floor — is the retrieval
layer of roadmap step 4.2 and is not here. Both then read each pick's details through
the ``Metadata`` port, which is what a card needs anyway and what makes the harness's
TMDb call counter say something real.
"""

import random
from collections.abc import Sequence

from tindarr.ports.metadata import Metadata, Title, TitleFilters
from tindarr.ports.titles import MediaKind
from tindarr.swipe.strategy import Candidate, StrategyContext

__all__ = ["PopularBaseline", "RandomBaseline", "eligible"]


def eligible(pool: Sequence[Title], context: StrategyContext) -> list[Title]:
    """Return the pool entries this user could legitimately be shown, in a fixed order.

    Three things are dropped: what the context says is excluded (voted on, served,
    owned), what the media type asks against, and what the household's content filters
    refuse. The filters applied here are the ones a title carries on its own —
    ``exclude_adult``, ``min_year``, ``excluded_original_languages``. Genres are not:
    TMDb spells them as ids that only the adapter can resolve, and resolving them is
    the retrieval layer's job, not a baseline's.
    """
    excluded = context.excluded
    filters = context.filters
    kept = [
        title
        for title in pool
        if title.ref not in excluded
        and _wanted_kind(title.ref.kind, context.media_kind)
        and _passes(title, filters)
    ]
    # A stable order before anything ranks it: two runs must see the same pool.
    kept.sort(key=lambda title: title.ref)
    return kept


def _wanted_kind(kind: MediaKind, wanted: MediaKind | None) -> bool:
    return wanted is None or kind == wanted


def _passes(title: Title, filters: TitleFilters) -> bool:
    if filters.exclude_adult and title.adult:
        return False
    if filters.min_year is not None and (title.year is None or title.year < filters.min_year):
        return False
    language = title.original_language
    return not (language is not None and language in filters.excluded_original_languages)


class PopularBaseline:
    """The most popular thing the user has not voted on yet.

    This is the floor ADR 0013 is arguing against, distilled: it knows nothing about
    taste and everything about fame, so it should score well on likes and badly on
    already-seen. If a real strategy cannot beat it on the already-seen rate, it has
    not earned its LLM bill.
    """

    name = "popular"

    def __init__(self, pool: Sequence[Title], metadata: Metadata) -> None:
        self._pool = tuple(pool)
        self._metadata = metadata

    async def propose(self, context: StrategyContext, size: int) -> Sequence[Candidate]:
        """Return the ``size`` most popular eligible titles, most popular first."""
        candidates = eligible(self._pool, context)
        # Negated popularity first, then the ref: ties must not depend on sort stability
        # across a pool that was built in a different order.
        candidates.sort(key=lambda title: (-title.popularity, title.ref))
        return [await self._card(title, context) for title in candidates[:size]]

    async def _card(self, title: Title, context: StrategyContext) -> Candidate:
        details = await self._metadata.details(title.ref, context.language)
        return Candidate(ref=title.ref, pick="safe", details=details)


class RandomBaseline:
    """Whatever the pool happens to hold, drawn without replacement.

    The other end of the floor. Nothing should ever score below it, and a strategy that
    only just beats it is choosing from too small a pool or ignoring what it knows.
    """

    name = "random"

    def __init__(self, pool: Sequence[Title], metadata: Metadata) -> None:
        self._pool = tuple(pool)
        self._metadata = metadata

    async def propose(self, context: StrategyContext, size: int) -> Sequence[Candidate]:
        """Return up to ``size`` eligible titles, drawn from the context's own seed."""
        candidates = eligible(self._pool, context)
        # Not a security decision: the seed is fixed by the caller precisely so that two
        # runs of this baseline produce the same batch.
        draw = random.Random(context.seed)  # noqa: S311
        draw.shuffle(candidates)
        return [
            Candidate(
                ref=title.ref,
                pick="explore",
                details=await self._metadata.details(title.ref, context.language),
            )
            for title in candidates[:size]
        ]
