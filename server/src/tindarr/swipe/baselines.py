"""Two strategies that are deliberately stupid, so the harness has a floor to beat.

Neither is meant to ship. They exist because a metric with nothing to compare it to is
decoration: "63 % of the new cards were liked" only means something next to what picking
the most popular unseen title, or picking at random, would have scored on the same
votes. They are also the proof that the port of ``tindarr.swipe.strategy`` can be
implemented at all without a database, a clock or an HTTP framework.

Both draw from the **same candidate pool as everything else** (``tindarr.swipe.retrieval``
since lot 4b): titles TMDb recommends to somebody who liked what this user liked, plus
filtered discovery under the novelty band. That is the point of a floor. While the
baselines drew from the fixture's own catalogue — which on a vote set built from real
votes holds exactly the 99 titles somebody voted on — they were picking from a shortlist
of scoreable titles, and "is the model better than picking the most popular thing?" was
being asked of two strategies that had been handed different questions.

Both then read each pick's details through the ``Metadata`` port, which is what a card
needs anyway and what makes the harness's TMDb call counter say something real.

Neither trusts the pool to be clean, although it is: ``eligible`` re-applies the
exclusions on the way out. A floor that would happily serve a title the retrieval layer
should have dropped is a floor that cannot catch a retrieval bug.
"""

import random
from collections.abc import Sequence

from tindarr.ports.metadata import Metadata, Title
from tindarr.ports.titles import MediaKind
from tindarr.swipe.retrieval import PoolSource, card_details, passes_filters
from tindarr.swipe.strategy import Candidate, StrategyContext

__all__ = ["PopularBaseline", "RandomBaseline", "eligible"]


def eligible(pool: Sequence[Title], context: StrategyContext) -> list[Title]:
    """Return the pool entries this user could legitimately be shown, in a fixed order.

    Three things are dropped: what the context says is excluded (voted on, served,
    owned), what the media type asks against, and what the household's content filters
    refuse — through ``passes_filters``, the same predicate the retrieval layer applies,
    so the floors and the candidate cannot end up with different shortlists.

    Genres are the one thing this cannot re-check: a ``Title`` carries ids and only the
    retrieval layer has resolved the household's names to them. It has already dropped
    what they exclude.
    """
    excluded = context.excluded
    filters = context.filters
    kept = [
        title
        for title in pool
        if title.ref not in excluded
        and _wanted_kind(title.ref.kind, context.media_kind)
        and passes_filters(title, filters, frozenset())
    ]
    # A stable order before anything ranks it: two runs must see the same pool.
    kept.sort(key=lambda title: title.ref)
    return kept


def _wanted_kind(kind: MediaKind, wanted: MediaKind | None) -> bool:
    return wanted is None or kind == wanted


class PopularBaseline:
    """The most popular thing the user has not voted on yet.

    This is the floor ADR 0013 is arguing against, distilled: it knows nothing about
    taste and everything about fame, so it should score well on likes and badly on
    already-seen. If a real strategy cannot beat it on the already-seen rate, it has
    not earned its LLM bill.
    """

    name = "popular"

    def __init__(self, retrieval: PoolSource, metadata: Metadata) -> None:
        self._retrieval = retrieval
        self._metadata = metadata

    async def propose(self, context: StrategyContext, size: int) -> Sequence[Candidate]:
        """Return the ``size`` most popular eligible titles, most popular first."""
        candidates = eligible((await self._retrieval.pool(context)).titles, context)
        # Negated popularity first, then the ref: ties must not depend on sort stability
        # across a pool that was built in a different order.
        candidates.sort(key=lambda title: (-title.popularity, title.ref))
        return [await self._card(title, context) for title in candidates[:size]]

    async def _card(self, title: Title, context: StrategyContext) -> Candidate:
        details = await card_details(self._metadata, title.ref, context.language)
        return Candidate(ref=title.ref, pick="safe", details=details)


class RandomBaseline:
    """Whatever the pool happens to hold, drawn without replacement.

    The other end of the floor. Nothing should ever score below it, and a strategy that
    only just beats it is choosing from too small a pool or ignoring what it knows.
    """

    name = "random"

    def __init__(self, retrieval: PoolSource, metadata: Metadata) -> None:
        self._retrieval = retrieval
        self._metadata = metadata

    async def propose(self, context: StrategyContext, size: int) -> Sequence[Candidate]:
        """Return up to ``size`` eligible titles, drawn from the context's own seed."""
        candidates = eligible((await self._retrieval.pool(context)).titles, context)
        # Not a security decision: the seed is fixed by the caller precisely so that two
        # runs of this baseline produce the same batch.
        draw = random.Random(context.seed)  # noqa: S311
        draw.shuffle(candidates)
        return [
            Candidate(
                ref=title.ref,
                pick="explore",
                details=await card_details(self._metadata, title.ref, context.language),
            )
            for title in candidates[:size]
        ]
