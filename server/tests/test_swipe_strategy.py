"""The vote vocabulary, the strategy port, and the two baselines behind it."""

from datetime import UTC, datetime

import pytest

from tests.support.evaluation import FixedPool, InMemoryMetadata, title
from tindarr.ports.media_server import LibraryIndex, LibraryItem
from tindarr.ports.metadata import TitleFilters
from tindarr.ports.titles import TitleRef
from tindarr.swipe.baselines import PopularBaseline, RandomBaseline, eligible
from tindarr.swipe.strategy import Strategy, StrategyContext
from tindarr.swipe.votes import (
    NEW_VOTES,
    OPINION_VOTES,
    SEEN_VOTES,
    VOTE_VALUES,
    Vote,
    as_vote_value,
    sorted_votes,
)

pytestmark = pytest.mark.anyio

DAY = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def vote(tmdb_id: int, value: str, minute: int = 0) -> Vote:
    """One vote on a film, at a fixed moment."""
    checked = as_vote_value(value)
    assert checked is not None
    return Vote(at=DAY.replace(minute=minute), ref=TitleRef("movie", tmdb_id), value=checked)


def test_the_five_vote_values_are_grouped_without_overlap() -> None:
    assert set(VOTE_VALUES) == {"like", "dislike", "seen_liked", "seen_disliked", "skip"}
    assert SEEN_VOTES | NEW_VOTES == OPINION_VOTES
    assert not SEEN_VOTES & NEW_VOTES
    # ``skip`` carries no opinion, which is what keeps it out of every rate.
    assert "skip" not in OPINION_VOTES


@pytest.mark.parametrize("value", ["seen", "LIKE", "", None, 1])
def test_a_vote_value_another_program_wrote_is_narrowed(value: object) -> None:
    assert as_vote_value(value) is None


def test_votes_sort_by_time_then_by_title() -> None:
    late, early = vote(2, "like", minute=5), vote(1, "dislike", minute=1)
    assert sorted_votes([late, early]) == (early, late)
    assert early.positive is False
    assert vote(3, "seen_liked").seen is True
    assert vote(3, "like").seen is False


def test_the_context_answers_what_a_strategy_may_know() -> None:
    context = StrategyContext(
        user_id="u1",
        history=(vote(1, "like"), vote(2, "dislike", 1), vote(3, "seen_liked", 2)),
        served=frozenset({TitleRef("movie", 4)}),
        library=LibraryIndex([LibraryItem("movie", "i5", "Five", tmdb_id=5)]),
    )
    assert context.liked == (TitleRef("movie", 1), TitleRef("movie", 3))
    assert context.disliked == (TitleRef("movie", 2),)
    assert context.voted == {TitleRef("movie", n) for n in (1, 2, 3)}
    assert context.excluded == {TitleRef("movie", n) for n in (1, 2, 3, 4, 5)}


def test_eligible_drops_what_the_context_forbids() -> None:
    pool = [
        title(1, "Voted"),
        title(2, "Served"),
        title(3, "Owned"),
        title(4, "Adult", adult=True),
        title(5, "Old", year=1974),
        title(6, "Russian", language="ru"),
        title(7, "Series", kind="tv"),
        title(8, "Keeper"),
    ]
    context = StrategyContext(
        user_id="u1",
        media_kind="movie",
        history=(vote(1, "like"),),
        served=frozenset({TitleRef("movie", 2)}),
        library=LibraryIndex([LibraryItem("movie", "i3", "Owned", tmdb_id=3)]),
        filters=TitleFilters(min_year=2000, excluded_original_languages=frozenset({"ru"})),
    )
    assert [row.ref.tmdb_id for row in eligible(pool, context)] == [8]


def test_both_media_kinds_are_kept_when_none_is_asked_for() -> None:
    pool = [title(1, "Film"), title(2, "Series", kind="tv")]
    kept = eligible(pool, StrategyContext(user_id="u1"))
    assert {row.ref.kind for row in kept} == {"movie", "tv"}


async def test_the_popular_baseline_ranks_by_popularity_and_reads_details() -> None:
    pool = [title(1, "Low", popularity=1.0), title(2, "High", popularity=9.0)]
    metadata = InMemoryMetadata(pool)
    strategy: Strategy = PopularBaseline(FixedPool(pool), metadata)
    assert strategy.name == "popular"

    cards = await strategy.propose(StrategyContext(user_id="u1", language="fr"), 5)

    assert [card.ref.tmdb_id for card in cards] == [2, 1]
    assert all(card.pick == "safe" for card in cards)
    # A card needs its details, and reading them is what the harness counts as a cost.
    assert metadata.calls == ["details:movie:2:fr", "details:movie:1:fr"]
    assert cards[0].details is not None
    assert cards[0].details.ref == cards[0].ref


async def test_the_popular_baseline_returns_no_more_than_it_was_asked_for() -> None:
    pool = [title(index, f"T{index}", popularity=float(index)) for index in range(1, 10)]
    cards = await PopularBaseline(FixedPool(pool), InMemoryMetadata(pool)).propose(
        StrategyContext(user_id="u1"), 3
    )
    assert [card.ref.tmdb_id for card in cards] == [9, 8, 7]


async def test_equal_popularity_does_not_depend_on_the_pool_order() -> None:
    pool = [title(index, f"T{index}", popularity=5.0) for index in (3, 1, 2)]
    first = await PopularBaseline(FixedPool(pool), InMemoryMetadata(pool)).propose(
        StrategyContext(user_id="u1"), 3
    )
    second = await PopularBaseline(FixedPool(list(reversed(pool))), InMemoryMetadata(pool)).propose(
        StrategyContext(user_id="u1"), 3
    )
    assert [card.ref for card in first] == [card.ref for card in second]


async def test_the_random_baseline_is_a_function_of_its_seed() -> None:
    pool = [title(index, f"T{index}") for index in range(1, 20)]
    strategy = RandomBaseline(FixedPool(pool), InMemoryMetadata(pool))
    same = await strategy.propose(StrategyContext(user_id="u1", seed=7), 5)
    again = await RandomBaseline(FixedPool(pool), InMemoryMetadata(pool)).propose(
        StrategyContext(user_id="u1", seed=7), 5
    )
    other = await strategy.propose(StrategyContext(user_id="u1", seed=8), 5)

    assert [card.ref for card in same] == [card.ref for card in again]
    assert [card.ref for card in same] != [card.ref for card in other]
    assert all(card.pick == "explore" for card in same)


async def test_a_baseline_proposes_nothing_when_the_pool_is_exhausted() -> None:
    pool = [title(1, "Only")]
    context = StrategyContext(user_id="u1", history=(vote(1, "like"),))
    assert await PopularBaseline(FixedPool(pool), InMemoryMetadata(pool)).propose(context, 5) == []
    assert await RandomBaseline(FixedPool(pool), InMemoryMetadata(pool)).propose(context, 5) == []
