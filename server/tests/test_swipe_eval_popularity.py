"""The popularity profile: the one number an open candidate pool cannot dilute.

Every other rate in the harness needs somebody to have voted on the card. These do not,
which is what they are for — so the tests here are mostly about the two ways such a
number could lie: reading fame from what the *strategy* said about its picks rather than
from the pool, and quietly reporting a profile when nothing recorded a pool at all.
"""

import pytest

from tests.support.evaluation import InMemoryMetadata, title
from tindarr.ports.metadata import Title
from tindarr.ports.titles import TitleRef
from tindarr.swipe.evaluation.popularity import (
    FameSample,
    PoolWatcher,
    profile,
    quantile,
    spread,
)
from tindarr.swipe.retrieval import NOVELTY_BANDS, CandidatePool, Retrieval
from tindarr.swipe.strategy import StrategyContext

pytestmark = pytest.mark.anyio


def famous(tmdb_id: int, popularity: float, votes: int) -> Title:
    """One candidate, described only by the two numbers fame is read from."""
    return Title(
        ref=TitleRef("movie", tmdb_id), title=f"T{tmdb_id}", popularity=popularity, vote_count=votes
    )


def test_quantile_returns_an_observation_and_never_an_average() -> None:
    """A median between two cards is a popularity no title has; the lower one is real."""
    assert quantile([1.0, 2.0, 3.0], 0.5) == 2.0
    assert quantile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    assert quantile([1.0, 2.0, 3.0, 4.0, 5.0], 0.75) == 4.0
    # Half up, not to the even neighbour: four cards take the third, not the second.
    assert quantile([1.0, 2.0, 3.0, 4.0], 0.5) == 3.0
    assert quantile([], 0.5) is None


def test_the_profile_reads_fame_from_the_pool_and_not_from_the_card() -> None:
    """A card is worth what the pool said it was worth, whatever the strategy claims."""
    pool = CandidatePool(
        titles=(famous(1, 10.0, 100), famous(2, 90.0, 900)),
        band=NOVELTY_BANDS["balanced"],
    )
    sample = profile(pool, [TitleRef("movie", 2)])
    assert [card.popularity for card in sample.cards] == [90.0]
    assert sample.floor == NOVELTY_BANDS["balanced"].popularity_floor
    assert len(sample.pool) == 2


def test_a_card_the_pool_never_offered_is_not_profiled() -> None:
    """It is left out rather than guessed at, and the report prints how many were placed."""
    pool = CandidatePool(titles=(famous(1, 10.0, 100),))
    sample = profile(pool, [TitleRef("movie", 7)])
    assert sample.cards == ()
    assert sample.floor is None


def test_no_pool_at_all_means_no_profile() -> None:
    """A replay driven without a watcher reports nothing, rather than reporting zero."""
    assert profile(None, [TitleRef("movie", 1)]) == FameSample()
    assert spread([FameSample()])["popularity_median"] is None
    assert spread([])["above_floor"] is None


def test_the_spread_pools_the_run_rather_than_averaging_its_batches() -> None:
    """A median of medians is not a median, and a deck is read whole."""
    first = FameSample(cards=(famous(1, 1.0, 10), famous(2, 3.0, 30)), pool=(famous(1, 1.0, 10),))
    second = FameSample(cards=(famous(3, 5.0, 50),), pool=(famous(3, 5.0, 50),))
    found = spread([first, second])
    assert found["popularity_median"] == 3.0
    assert found["popularity_p75"] == 5.0
    assert found["vote_count_median"] == 30.0
    assert found["pool_popularity_median"] == 5.0


def test_the_share_above_the_floor_counts_only_batches_whose_band_is_known() -> None:
    """A pool that did not say which band built it cannot say what the floor was."""
    band = NOVELTY_BANDS["familiar"]
    graded = FameSample(cards=(famous(1, 1.0, 10), famous(2, 9.0, 90)), floor=band.popularity_floor)
    assert spread([graded, FameSample(cards=(famous(3, 0.0, 1),))])["above_floor"] == 0.5


async def test_the_watcher_hands_back_the_pool_it_recorded() -> None:
    """A pure observer: what the strategy gets is what retrieval returned, unchanged."""
    metadata = InMemoryMetadata(titles=[title(1, "One", popularity=300.0)])
    watcher = PoolWatcher()
    source = watcher.watching(Retrieval(metadata))
    context = StrategyContext(user_id="user-1", batch_index=2)
    found = await source.pool(context)
    assert watcher.offered("user-1", 2) is found
    assert watcher.offered("user-1", 3) is None
    assert watcher.offered("user-2", 2) is None


async def test_the_recorded_pool_carries_the_band_that_built_it() -> None:
    """The prompt and the filter read one band, so they cannot start disagreeing."""
    metadata = InMemoryMetadata(titles=[title(1, "One", popularity=300.0)])
    found = await Retrieval(metadata).pool(
        StrategyContext(user_id="user-1", novelty="bold", history=(), batch_index=0)
    )
    # A first batch is calibration, and calibration reads the familiar band whatever the
    # user asked for — the context decides it, not the strategy.
    assert found.band == NOVELTY_BANDS["familiar"]
