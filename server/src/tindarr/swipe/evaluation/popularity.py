"""How famous the cards a strategy proposed were, measured without a single vote.

Every other number in the harness needs the fixture to have an opinion about a card.
With an open candidate pool it rarely has one — coverage is a few per cent on the real
vote set — so the question ADR 0013 is actually about, *"is this strategy serving the
wall of blockbusters again?"*, cannot be answered from the votes alone.

This can. TMDb says how popular a title is and how many people ever voted on it, for
every candidate, whether or not anybody in the fixture ever saw it. So the profile below
compares two distributions that always exist:

- **the cards a strategy proposed**, and
- **the pool it chose them from**, recorded as the retrieval layer handed it over.

A strategy that takes the famous end of the pool has a higher median than its own pool;
one that spreads evenly matches it. That comparison is what tells a *ranking* bias apart
from a *retrieval* one, and neither half of it depends on anybody having voted.

**The numbers are observed, not claimed.** ``PoolWatcher`` sits between the retrieval
layer and the strategy and records what came out of retrieval; the popularity of a card
is then read from that record, never from what the strategy handed back about its own
picks. A strategy cannot make itself look obscure by mislabelling a card, only by
choosing a different one — which is the point.

They are printed and never graded. A popularity that is "too low" is as much a defect as
one that is too high — the floor exists because a deck of listings and home videos is
not a deck — so there is no direction to fail a build on, and a diagnostic that becomes
a target stops diagnosing. What the gate holds is the fault counts and the recall.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tindarr.ports.metadata import Title
from tindarr.ports.titles import TitleRef
from tindarr.swipe.retrieval import CandidatePool, PoolSource
from tindarr.swipe.strategy import StrategyContext

__all__ = ["FameSample", "PoolWatcher", "profile", "quantile", "spread"]


@dataclass(frozen=True, slots=True)
class FameSample:
    """One batch's cards and the pool they were chosen from, as TMDb describes them.

    Empty when nothing recorded the pool — a replay driven without a watcher, or a
    strategy that proposed a title its pool never offered. Either way the batch simply
    contributes nothing to the profile, and the report says how many cards it could
    place.
    """

    #: The proposed cards, as the pool described them. Usable cards only: a duplicate or
    #: a title the user already voted on is waste, and waste has no taste to profile.
    cards: tuple[Title, ...] = ()
    #: Everything that batch could have been chosen from, in the pool's own order.
    pool: tuple[Title, ...] = ()
    #: ADR 0013's adaptive popularity floor for this batch, or ``None`` when the pool
    #: did not say which band built it.
    floor: float | None = None


def profile(pool: CandidatePool | None, refs: Sequence[TitleRef]) -> FameSample:
    """Return what ``refs`` and ``pool`` were worth in fame, for one batch."""
    if pool is None:
        return FameSample()
    offered = pool.by_ref
    found = tuple(title for ref in refs if (title := offered.get(ref)) is not None)
    return FameSample(
        cards=found,
        pool=tuple(pool.titles),
        floor=pool.band.popularity_floor if pool.band is not None else None,
    )


def quantile(values: Sequence[float], share: float) -> float | None:
    """Return the value at ``share`` of a sorted sample, or ``None`` when it is empty.

    The plain nearest-rank definition, rounding half up, spelled out here rather than
    imported: the ``statistics`` module interpolates between two observations, which
    invents a popularity no title has. A median that is somebody's actual popularity is
    easier to check against TMDb by hand, and with tens of cards the difference between
    the two definitions is noise. ``round`` is avoided for the same reason: it rounds a
    half to the even neighbour, so a sample of four would take its second card as the
    median and a sample of six its third.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = min(len(ordered) - 1, max(0, int(share * (len(ordered) - 1) + 0.5)))
    return ordered[rank]


class PoolWatcher:
    """Records the pool every batch was offered, so the harness can profile the picks.

    A pure observer: it delegates to the real ``PoolSource``, hands back exactly what it
    got and keeps a reference. Nothing it records is ever read back into a strategy, so
    one watcher for a whole run cannot carry one person's state into another's batch the
    way a shared *cache* could — and the batches it files are keyed by user and batch
    index, which is what the replay looks them up by.
    """

    def __init__(self) -> None:
        self._offered: dict[tuple[str, int], CandidatePool] = {}

    def watching(self, source: PoolSource) -> PoolSource:
        """Return ``source`` with this watcher behind it, for one user's run."""
        return _Watched(source, self._offered)

    def offered(self, user_id: str, batch_index: int) -> CandidatePool | None:
        """Return the pool one batch was built from, or ``None`` if none was recorded."""
        return self._offered.get((user_id, batch_index))


class _Watched:
    """One user's pool source, filing what it returns with the watcher."""

    __slots__ = ("_offered", "_source")

    def __init__(self, source: PoolSource, offered: dict[tuple[str, int], CandidatePool]) -> None:
        self._source = source
        self._offered = offered

    async def pool(self, context: StrategyContext) -> CandidatePool:
        """Return the real pool, having noted which batch was offered it."""
        found = await self._source.pool(context)
        self._offered[(context.user_id, context.batch_index)] = found
        return found


def spread(samples: Sequence[FameSample]) -> Mapping[str, float | None]:
    """Aggregate a run's batches into the popularity profile the report prints.

    Pooled over the run rather than averaged over the batches: a median of medians is
    not a median, and the question is what a person would have been shown across the
    whole deck.
    """
    cards = [title for sample in samples for title in sample.cards]
    offered = [title for sample in samples for title in sample.pool]
    above = [
        title.popularity >= sample.floor
        for sample in samples
        if sample.floor is not None
        for title in sample.cards
    ]
    return {
        "popularity_median": quantile([title.popularity for title in cards], 0.5),
        "popularity_p75": quantile([title.popularity for title in cards], 0.75),
        "pool_popularity_median": quantile([title.popularity for title in offered], 0.5),
        "vote_count_median": quantile([float(title.vote_count) for title in cards], 0.5),
        "pool_vote_count_median": quantile([float(title.vote_count) for title in offered], 0.5),
        "above_floor": (sum(above) / len(above)) if above else None,
    }
