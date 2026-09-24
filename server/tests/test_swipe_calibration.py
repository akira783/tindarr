"""The calibration grid: what goes on the wall, and what is never asked about twice.

ADR 0013's distribution of already-seen cards is the specification — 1 from the 1980s,
8 from the 1990s, 6 from the 2000s, 22 from the 2010s, 10 from the 2020s — so the tests
here are mostly about *spread*: a wall of thirty films from one decade, one genre or one
language asks one question thirty times.
"""

from datetime import UTC, datetime

import pytest

from tests.support.evaluation import InMemoryMetadata
from tindarr.core.errors import ProblemError
from tindarr.ports.metadata import DiscoverQuery, Title, TitleFilters
from tindarr.ports.titles import MediaKind, TitleRef
from tindarr.swipe.calibration import (
    ERAS,
    GRID_MIN_VOTES,
    CalibrationGrid,
    GridRequest,
    GridTick,
    grid_history,
)

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def famous(  # noqa: PLR0913 - a title is a bag of fields; naming them beats a builder
    tmdb_id: int,
    *,
    kind: MediaKind = "movie",
    year: int = 2015,
    votes: int = 20_000,
    genre: int = 28,
    language: str = "en",
) -> Title:
    """A title a lot of people have an opinion about."""
    return Title(
        ref=TitleRef(kind, tmdb_id),
        title=f"T{tmdb_id}",
        year=year,
        vote_count=votes,
        popularity=50.0,
        original_language=language,
        genre_ids=(genre,),
        poster_path=f"/p{tmdb_id}.jpg",
    )


class Discovery(InMemoryMetadata):
    """Answers every slice with titles numbered after the era it asked for."""

    async def discover(self, query: DiscoverQuery) -> list[Title]:
        self.calls.append(f"discover:{query.kind}:{query.from_year}:{query.original_language}")
        base = (query.from_year or 0) * 100 + (1 if query.kind == "tv" else 0)
        genre = 28 if query.from_year and query.from_year >= 2010 else 18
        return [
            famous(base + index, kind=query.kind, year=query.from_year or 2000, genre=genre)
            for index in range(2, 12)
        ]


class TestTheWall:
    """What a wall is made of."""

    async def test_it_spreads_across_the_decades_from_its_first_row(self) -> None:
        wall = await CalibrationGrid(Discovery()).build(GridRequest(size=10))
        decades = [(found.year or 0) // 10 for found in wall[:5]]
        assert len(set(decades)) == 5

    async def test_it_reads_one_slice_per_era_and_kind_twice_over(self) -> None:
        metadata = Discovery()
        await CalibrationGrid(metadata).build(GridRequest(language="fr"))
        # Two axes (the internationally famous, then the locally famous), five eras,
        # two kinds.
        assert len(metadata.calls) == 2 * len(ERAS) * 2

    async def test_an_english_household_asks_about_its_own_country_instead(self) -> None:
        metadata = Discovery()
        await CalibrationGrid(metadata).build(GridRequest(language="en", region="AU"))
        # "en" says nothing about where somebody lives, so the local axis is a country.
        assert all(call.endswith(":None") for call in metadata.calls)

    async def test_a_local_slice_asks_for_the_household_s_own_language(self) -> None:
        metadata = Discovery()
        await CalibrationGrid(metadata).build(GridRequest(language="fr-FR"))
        assert any(call.endswith(":fr") for call in metadata.calls)

    async def test_no_genre_takes_more_than_a_third_of_the_wall(self) -> None:
        wall = await CalibrationGrid(Discovery()).build(GridRequest(size=12))
        action = sum(1 for found in wall if found.ref.tmdb_id >= 201_000)
        assert action <= 12

    async def test_it_never_returns_more_than_it_was_asked_for(self) -> None:
        wall = await CalibrationGrid(Discovery()).build(GridRequest(size=7))
        assert len(wall) == 7

    async def test_the_page_travels_to_tmdb_so_a_second_wall_is_a_second_wall(self) -> None:
        pages_asked: list[int] = []

        class Paged(Discovery):
            async def discover(self, query: DiscoverQuery) -> list[Title]:
                pages_asked.append(query.page)
                return await super().discover(query)

        metadata = Paged()
        await CalibrationGrid(metadata).build(GridRequest(page=3))
        assert set(pages_asked) == {3}


class TestWhatIsNeverAsked:
    """A wall must not ask a question whose answer is already recorded."""

    async def test_a_title_already_known_is_gone_before_the_wall_is_built(self) -> None:
        wall = await CalibrationGrid(Discovery()).build(
            GridRequest(size=60, known=frozenset({TitleRef("movie", 201_502)}))
        )
        assert TitleRef("movie", 201_502) not in {found.ref for found in wall}

    async def test_an_obscure_title_is_not_worth_a_poster(self) -> None:
        class Obscure(InMemoryMetadata):
            async def discover(self, query: DiscoverQuery) -> list[Title]:
                return [famous(1, votes=GRID_MIN_VOTES - 1)]

        assert await CalibrationGrid(Obscure()).build(GridRequest()) == ()

    async def test_the_household_s_content_filters_are_honoured(self) -> None:
        class Horror(InMemoryMetadata):
            async def discover(self, query: DiscoverQuery) -> list[Title]:
                return [famous(1, genre=27), famous(2, genre=18)]

        wall = await CalibrationGrid(Horror()).build(
            GridRequest(filters=TitleFilters(excluded_genres=frozenset({"27"})))
        )
        assert [found.ref.tmdb_id for found in wall] == [2]

    async def test_the_asked_for_media_type_is_the_only_one_read(self) -> None:
        metadata = Discovery()
        await CalibrationGrid(metadata).build(GridRequest(media_kind="movie"))
        assert all(":movie:" in call for call in metadata.calls)


class TestFailures:
    """A slice TMDb refused costs one decade, not the wall."""

    async def test_one_dead_slice(self) -> None:
        class Flaky(Discovery):
            async def discover(self, query: DiscoverQuery) -> list[Title]:
                if query.from_year == ERAS[0][0]:
                    raise ProblemError(502, "metadata_unreachable")
                return await super().discover(query)

        wall = await CalibrationGrid(Flaky()).build(GridRequest(size=10))
        assert wall
        assert all((found.year or 0) < ERAS[0][0] for found in wall)

    async def test_an_empty_answer_everywhere(self) -> None:
        assert await CalibrationGrid(InMemoryMetadata()).build(GridRequest()) == ()

    async def test_a_genre_filter_tmdb_will_not_resolve_fails_closed(self) -> None:
        class Silent(Discovery):
            async def excluded_genre_ids(self, filters: TitleFilters) -> frozenset[int]:
                raise ProblemError(502, "metadata_unreachable")

        with pytest.raises(ProblemError):
            await CalibrationGrid(Silent()).build(
                GridRequest(filters=TitleFilters(excluded_genres=frozenset({"horror"})))
            )


class TestTicks:
    """What an answer becomes, and what it deliberately does not."""

    def test_a_tick_is_history_and_not_a_vote(self) -> None:
        rows = grid_history([GridTick(TitleRef("movie", 27205), seen=True)], now=NOW)
        assert rows[0].source == "grid"
        assert rows[0].seen
        # No state: the person said they had seen it, not that they finished it, so
        # nothing here becomes a taste signal it was never given.
        assert rows[0].state is None
        assert rows[0].as_engagement() is None

    def test_a_no_is_stored_too(self) -> None:
        rows = grid_history([GridTick(TitleRef("movie", 1), seen=False)], now=NOW)
        assert not rows[0].seen

    def test_the_last_answer_about_one_title_wins(self) -> None:
        rows = grid_history(
            [GridTick(TitleRef("movie", 1), seen=True), GridTick(TitleRef("movie", 1), seen=False)],
            now=NOW,
        )
        assert len(rows) == 1
        assert not rows[0].seen
