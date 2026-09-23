"""The retrieval layer: what goes into a candidate pool, and what never gets near one.

The property these tests are about is the one ADR 0013 turns on: **every exclusion is
applied to the pool, before the model sees it.** A test that only checked the strategy's
output would pass just as happily with the filtering done afterwards, which is the
design the ADR rejected — so most of what follows reads ``pool()`` directly.
"""

from collections.abc import Mapping

import pytest

from tests.support.evaluation import InMemoryMetadata, title
from tests.test_swipe_strategy import DAY
from tindarr.core.errors import ProblemError
from tindarr.ports.media_server import LibraryIndex, LibraryItem
from tindarr.ports.metadata import DiscoverQuery, Title, TitleFilters
from tindarr.ports.titles import MediaKind, TitleRef
from tindarr.swipe.retrieval import MAX_SEEDS, NOVELTY_BANDS, Retrieval
from tindarr.swipe.strategy import StrategyContext
from tindarr.swipe.votes import Vote, VoteValue

pytestmark = pytest.mark.anyio


def known(tmdb_id: int, *, popularity: float = 50.0, votes: int = 5000, **rest: object) -> Title:
    """A candidate comfortably inside every band, unless a test says otherwise."""
    built = title(tmdb_id, f"T{tmdb_id}", popularity=popularity, **rest)  # pyright: ignore[reportArgumentType]
    return Title(
        ref=built.ref,
        title=built.title,
        year=built.year,
        popularity=built.popularity,
        original_language=built.original_language,
        adult=built.adult,
        genre_ids=built.genre_ids,
        vote_count=votes,
        vote_average=7.0,
    )


def vote(tmdb_id: int, value: str, minute: int = 0) -> Vote:
    """One vote on a film, at a fixed moment."""
    checked: VoteValue = value  # pyright: ignore[reportAssignmentType]
    return Vote(at=DAY.replace(minute=minute), ref=TitleRef("movie", tmdb_id), value=checked)


async def test_everything_the_context_excludes_is_gone_before_the_model_could_see_it() -> None:
    metadata = InMemoryMetadata(
        pages={("movie", page): [known(n) for n in (1, 2, 3, 4, 5)] for page in (1, 2, 3)}
    )
    context = StrategyContext(
        user_id="u1",
        media_kind="movie",
        history=(vote(1, "like"), vote(2, "dislike", 1)),
        served=frozenset({TitleRef("movie", 3)}),
        library=LibraryIndex([LibraryItem("movie", "i4", "Four", tmdb_id=4)]),
    )

    pool = await Retrieval(metadata).pool(context)

    assert [row.ref.tmdb_id for row in pool.titles] == [5]


async def test_the_household_filters_are_applied_to_the_pool_too() -> None:
    metadata = InMemoryMetadata(
        pages={
            ("movie", page): [
                known(1, adult=True),
                known(2, year=1974),
                known(3, language="ru"),
                known(4, genre_ids=(27,)),
                known(5),
            ]
            for page in (1, 2, 3)
        }
    )
    context = StrategyContext(
        user_id="u1",
        media_kind="movie",
        filters=TitleFilters(
            min_year=2000,
            excluded_original_languages=frozenset({"ru"}),
            excluded_genres=frozenset({"27"}),
        ),
    )

    pool = await Retrieval(metadata).pool(context)

    assert [row.ref.tmdb_id for row in pool.titles] == [5]


async def test_a_title_outside_the_novelty_band_never_enters_the_pool() -> None:
    """The band applies to recommendations as well, which take no filter of their own."""
    seed = TitleRef("movie", 900)
    metadata = InMemoryMetadata(
        pages={},
        recommendations={seed: [known(1, votes=10), known(2, popularity=0.1), known(3)]},
    )
    context = StrategyContext(
        user_id="u1", media_kind="movie", novelty="familiar", history=(vote(900, "like"),)
    )

    pool = await Retrieval(metadata).pool(context)

    # 1 is below the familiar band's 600 votes, 2 below its popularity floor of 5.
    assert [row.ref.tmdb_id for row in pool.titles] == [3]


async def test_the_popularity_floor_drops_as_the_novelty_setting_rises() -> None:
    """ADR 0013's adaptive floor, as a number rather than as a sentence in a prompt."""
    floors = [NOVELTY_BANDS[level].popularity_floor for level in ("familiar", "balanced", "bold")]

    assert floors == sorted(floors, reverse=True)
    # ...and the fame ceiling only exists at the bold end, where "they have probably
    # seen it" is the thing being avoided.
    assert NOVELTY_BANDS["familiar"].max_votes is None
    assert NOVELTY_BANDS["bold"].max_votes is not None


async def test_the_seeds_are_the_most_recent_likes_and_there_are_not_many() -> None:
    liked = [TitleRef("movie", 900 + index) for index in range(MAX_SEEDS + 3)]
    metadata = InMemoryMetadata(pages={}, recommendations=dict.fromkeys(liked, ()))
    history = tuple(vote(ref.tmdb_id, "like", minute=index) for index, ref in enumerate(liked))

    await Retrieval(metadata).pool(
        StrategyContext(user_id="u1", media_kind="movie", history=history)
    )

    asked = [call for call in metadata.calls if call.startswith("related:")]
    assert len(asked) == MAX_SEEDS
    # Newest first: the last vote cast is the first seed.
    assert asked[0] == f"related:movie:{liked[-1].tmdb_id}:1"


async def test_a_seed_tmdb_will_not_answer_for_does_not_cost_the_batch() -> None:
    class Grumpy(InMemoryMetadata):
        async def related(self, ref: TitleRef, language: str, page: int = 1) -> list[Title]:
            raise ProblemError(502, "metadata_unreachable", "no")

    metadata = Grumpy(pages={("movie", page): [known(7)] for page in (1, 2, 3)})
    context = StrategyContext(user_id="u1", media_kind="movie", history=(vote(900, "like"),))

    pool = await Retrieval(metadata).pool(context)

    assert [row.ref.tmdb_id for row in pool.titles] == [7]


async def test_a_discovery_page_tmdb_will_not_answer_does_not_cost_the_batch() -> None:
    class Grumpy(InMemoryMetadata):
        async def discover(self, query: DiscoverQuery) -> list[Title]:
            raise ProblemError(502, "metadata_unreachable", "no")

    seed = TitleRef("movie", 900)
    metadata = Grumpy(recommendations={seed: [known(7)]})
    context = StrategyContext(user_id="u1", media_kind="movie", history=(vote(900, "like"),))

    pool = await Retrieval(metadata).pool(context)

    assert [row.ref.tmdb_id for row in pool.titles] == [7]


async def test_nothing_at_all_is_an_empty_pool_rather_than_a_failure() -> None:
    pool = await Retrieval(InMemoryMetadata(pages={})).pool(StrategyContext(user_id="u1"))

    assert len(pool) == 0
    assert pool.titles == ()


async def test_the_same_page_is_not_paid_for_twice_in_one_user_s_run() -> None:
    metadata = InMemoryMetadata(
        pages={("movie", page): [known(n) for n in (1, 2)] for page in (1, 2, 3)}
    )
    retrieval = Retrieval(metadata)
    context = StrategyContext(user_id="u1", media_kind="movie")

    await retrieval.pool(context)
    calls = len(metadata.calls)
    await retrieval.pool(StrategyContext(user_id="u1", media_kind="movie", batch_index=1))

    assert len(metadata.calls) == calls


async def test_a_calibration_pool_asks_for_fame_rather_than_for_novelty() -> None:
    """A calibration batch is trying to find out what somebody has already watched."""
    metadata = InMemoryMetadata(pages={("movie", page): [known(1)] for page in (1, 2, 3)})
    context = StrategyContext(user_id="u1", media_kind="movie", novelty="bold")

    await Retrieval(metadata).pool(context, calibration=True)
    calibrating = [call for call in metadata.calls if call.startswith("discover:")]
    metadata.calls.clear()
    await Retrieval(metadata).pool(context)
    bold = [call for call in metadata.calls if call.startswith("discover:")]

    # The familiar band reads pages 1-2; bold reads 2-4.
    assert calibrating == ["discover:movie:1", "discover:movie:2"]
    assert bold == ["discover:movie:2", "discover:movie:3", "discover:movie:4"]


async def test_the_pool_says_which_candidates_came_from_something_they_liked() -> None:
    seed = TitleRef("movie", 900)
    metadata = InMemoryMetadata(
        pages={("movie", page): [known(50), known(51)] for page in (1, 2, 3)},
        recommendations={seed: [known(1), known(2)]},
    )
    context = StrategyContext(user_id="u1", media_kind="movie", history=(vote(900, "like"),))

    pool = await Retrieval(metadata).pool(context)

    assert pool.origin[TitleRef("movie", 1)] == "safe"
    assert pool.origin[TitleRef("movie", 50)] == "explore"
    assert 0 < pool.seeded < len(pool)


async def test_two_identical_contexts_produce_the_same_pool() -> None:
    metadata = InMemoryMetadata(
        pages={("movie", page): [known(n) for n in (5, 3, 9, 1)] for page in (1, 2, 3)}
    )
    context = StrategyContext(user_id="u1", media_kind="movie")

    first = await Retrieval(metadata).pool(context)
    second = await Retrieval(metadata).pool(context)

    assert [row.ref for row in first.titles] == [row.ref for row in second.titles]


async def test_a_pool_is_capped_so_a_prompt_stays_a_prompt() -> None:
    many = [known(n) for n in range(1, 200)]
    metadata = InMemoryMetadata(pages={("movie", page): many for page in (1, 2, 3)})

    pool = await Retrieval(metadata, size=12).pool(
        StrategyContext(user_id="u1", media_kind="movie")
    )

    assert len(pool) == 12


async def test_the_media_type_a_user_asked_for_is_the_only_one_retrieved() -> None:
    pages: dict[tuple[MediaKind, int], list[Title]] = {
        ("movie", page): [known(1)] for page in (1, 2, 3)
    }
    pages |= {("tv", page): [known(2, kind="tv")] for page in (1, 2, 3)}
    metadata = InMemoryMetadata(pages=pages)

    pool = await Retrieval(metadata).pool(StrategyContext(user_id="u1", media_kind="tv"))

    assert [row.ref for row in pool.titles] == [TitleRef("tv", 2)]


async def test_a_search_is_never_part_of_building_a_pool() -> None:
    """The fork resolved titles by name. Nothing here does, and nothing here should."""
    metadata = InMemoryMetadata(pages={("movie", page): [known(1)] for page in (1, 2, 3)})

    await Retrieval(metadata).pool(StrategyContext(user_id="u1", media_kind="movie"))

    assert not [call for call in metadata.calls if call.startswith(("search", "match"))]


async def test_a_pool_carries_the_genre_names_a_listing_only_gives_ids_for() -> None:
    metadata = InMemoryMetadata(
        pages={("movie", page): [known(1, genre_ids=(878, 18))] for page in (1, 2, 3)},
        genre_names={878: "Science Fiction", 18: "Drama"},
    )

    pool = await Retrieval(metadata).pool(StrategyContext(user_id="u1", media_kind="movie"))

    assert pool.genre_names(pool.titles[0]) == ("Science Fiction", "Drama")


async def test_a_genre_list_tmdb_refuses_costs_the_names_and_not_the_pool() -> None:
    class Grumpy(InMemoryMetadata):
        async def genres(self) -> Mapping[int, str]:
            raise ProblemError(502, "metadata_unreachable", "no")

    metadata = Grumpy(pages={("movie", page): [known(1)] for page in (1, 2, 3)})

    pool = await Retrieval(metadata).pool(StrategyContext(user_id="u1", media_kind="movie"))

    assert len(pool) == 1
    assert pool.genre_names(pool.titles[0]) == ()
