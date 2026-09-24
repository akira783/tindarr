"""Identifying an import, and what it does rather than guess.

The property these tests exist for is the one every comparable project gets wrong:
``results[0]``. A row that does not clearly name one title must end in the review queue
and never in somebody's history, and a row that clearly does must not need a human.
"""

from datetime import UTC, datetime, timedelta

import pytest

from tests.support.evaluation import InMemoryMetadata, title
from tindarr.core.errors import ProblemError
from tindarr.ports.media_server import ABANDONED_AFTER_DAYS, MOSTLY_WATCHED_RATIO
from tindarr.ports.metadata import ExternalMatch, SearchQuery, Title, TitleDetails
from tindarr.ports.titles import TitleRef
from tindarr.swipe.imports.records import ParsedFile, WatchedItem
from tindarr.swipe.imports.resolve import CONFIDENT_SIMILARITY, MetadataDownError, TitleResolver
from tindarr.swipe.imports.run import ImportOutcome, resolve_import, watched_title

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def found(tmdb_id: int, name: str, *, kind: str = "tv", popularity: float = 10.0) -> Title:
    """A search result, with the two fields the ranking reads."""
    return title(tmdb_id, name, kind=kind, popularity=popularity)  # pyright: ignore[reportArgumentType]


def parsed(*items: WatchedItem, kind: str = "netflix") -> ParsedFile:
    """A parsed file holding exactly these rows."""
    return ParsedFile(format=kind, items=items)  # pyright: ignore[reportArgumentType]


class TestRanking:
    """Similarity decides; fame only ever breaks a tie."""

    async def test_an_exact_title_beats_a_more_popular_near_miss(self) -> None:
        metadata = InMemoryMetadata(
            search_results={
                "Away": [
                    found(1, "Away From Her", kind="movie", popularity=900.0),
                    found(2, "Away", popularity=3.0),
                ]
            }
        )
        resolution = await TitleResolver(metadata, "fr").resolve(WatchedItem("Away", "tv"))
        assert resolution.best is not None
        assert resolution.best.ref == TitleRef("tv", 2)
        assert resolution.confident

    async def test_fame_breaks_a_tie_between_two_equally_good_answers(self) -> None:
        metadata = InMemoryMetadata(
            search_results={
                "Skam": [found(1, "Skam", popularity=2.0), found(2, "Skam", popularity=80.0)]
            }
        )
        resolution = await TitleResolver(metadata, "fr").resolve(WatchedItem("Skam", "tv"))
        assert resolution.best is not None
        assert resolution.best.ref == TitleRef("tv", 2)

    async def test_the_file_s_own_guess_is_searched_first(self) -> None:
        metadata = InMemoryMetadata(search_results={"Fargo": [found(7, "Fargo")]})
        await TitleResolver(metadata, "en").resolve(WatchedItem("Fargo", "tv"))
        assert metadata.calls[0] == "search:Fargo"

    async def test_the_second_spelling_is_the_part_before_a_french_subtitle(self) -> None:
        metadata = InMemoryMetadata(
            search_results={"The Witcher": [found(3, "The Witcher", popularity=40.0)]}
        )
        item = WatchedItem("The Witcher : Les sirènes des abysses", "tv")
        resolution = await TitleResolver(metadata, "fr").resolve(item)
        assert resolution.best is not None
        assert resolution.best.ref == TitleRef("tv", 3)
        # The row as written was tried first and found nothing.
        assert metadata.calls[0].startswith("search:The Witcher : ")

    async def test_an_exact_answer_stops_the_remaining_spellings(self) -> None:
        metadata = InMemoryMetadata(search_results={"Dune: Part Two": [found(1, "Dune: Part Two")]})
        await TitleResolver(metadata, "en").resolve(WatchedItem("Dune: Part Two", "movie"))
        assert metadata.calls == ["search:Dune: Part Two"]

    async def test_the_same_search_is_never_paid_for_twice(self) -> None:
        metadata = InMemoryMetadata(search_results={"Heroes": [found(1, "Heroes")]})
        resolver = TitleResolver(metadata, "en")
        await resolver.resolve(WatchedItem("Heroes", "tv"))
        await resolver.resolve(WatchedItem("Heroes", "tv"))
        assert metadata.calls.count("search:Heroes") == 1


class TestAbstention:
    """A row that does not clearly name one title becomes a question."""

    async def test_a_weak_match_is_offered_rather_than_written_down(self) -> None:
        metadata = InMemoryMetadata(
            search_results={"Mushoku Tensei": [found(9, "Mushoku Tensei: Jobless Reincarnation")]}
        )
        item = WatchedItem("Mushoku Tensei", "tv")
        resolution = await TitleResolver(metadata, "fr").resolve(item)
        assert resolution.best is not None
        assert resolution.best.similarity < CONFIDENT_SIMILARITY
        assert not resolution.confident

    async def test_a_row_tmdb_knows_nothing_about_has_no_candidate(self) -> None:
        resolution = await TitleResolver(InMemoryMetadata(), "fr").resolve(
            WatchedItem("Emergência Radioativa", "tv")
        )
        assert resolution.best is None
        assert not resolution.confident

    async def test_an_empty_row_asks_tmdb_nothing(self) -> None:
        metadata = InMemoryMetadata()
        resolution = await TitleResolver(metadata, "fr").resolve(WatchedItem("   "))
        assert resolution.best is None
        assert metadata.calls == []

    async def test_the_alternatives_are_the_other_candidates_best_first(self) -> None:
        metadata = InMemoryMetadata(
            search_results={
                "Pluto": [
                    found(1, "Plutonium", popularity=1.0),
                    found(2, "Pluto TV", popularity=5.0),
                    found(3, "Pluto's Blues", popularity=2.0),
                ]
            }
        )
        resolution = await TitleResolver(metadata, "fr").resolve(WatchedItem("Pluto", "tv"))
        assert not resolution.confident
        assert [entry.ref.tmdb_id for entry in resolution.alternatives] == [1, 3]


class TestExternalIds:
    """An IMDb id is resolved exactly, and never searched for."""

    async def test_a_film_id(self) -> None:
        metadata = InMemoryMetadata(
            external={"tt0816692": ExternalMatch(TitleRef("movie", 157336), "Interstellar")}
        )
        item = WatchedItem("Interstellar", "movie", imdb_id="tt0816692")
        resolution = await TitleResolver(metadata, "en").resolve(item)
        assert resolution.best is not None
        assert resolution.best.ref == TitleRef("movie", 157336)
        assert resolution.exact
        assert not any(call.startswith("search:") for call in metadata.calls)

    async def test_an_episode_id_resolves_to_its_series(self) -> None:
        metadata = InMemoryMetadata(
            external={
                "tt2178784": ExternalMatch(
                    TitleRef("tv", 1399), "The Rains of Castamere", episode=True
                )
            }
        )
        item = WatchedItem("The Rains of Castamere", "tv", imdb_id="tt2178784", episodes=1)
        resolution = await TitleResolver(metadata, "en").resolve(item)
        assert resolution.episode
        assert resolution.best is not None
        assert resolution.best.ref == TitleRef("tv", 1399)

    async def test_an_id_tmdb_does_not_know_is_not_queued_for_review(self) -> None:
        metadata = InMemoryMetadata()
        item = WatchedItem("Whatever", "movie", imdb_id="tt9999999")
        resolution = await TitleResolver(metadata, "en").resolve(item)
        assert resolution.best is None

    async def test_a_failing_find_costs_one_row(self) -> None:
        class Grumpy(InMemoryMetadata):
            async def find_imdb(self, imdb_id: str, language: str) -> ExternalMatch | None:
                raise ProblemError(502, "metadata_unreachable")

        resolution = await TitleResolver(Grumpy(), "en").resolve(
            WatchedItem("X", "movie", imdb_id="tt1")
        )
        assert resolution.best is None


class TestGivingUp:
    """A dead TMDb must not turn a file into a queue of questions about nothing."""

    async def test_several_refusals_in_a_row_abandon_the_run(self) -> None:
        class Dead(InMemoryMetadata):
            async def search(self, query: SearchQuery) -> list[Title]:
                raise ProblemError(502, "metadata_unreachable")

        resolver = TitleResolver(Dead(), "en")

        async def whole_file() -> None:
            for index in range(10):
                await resolver.resolve(WatchedItem(f"Row {index}", "movie"))

        with pytest.raises(MetadataDownError):
            await whole_file()

    async def test_one_refusal_between_two_answers_does_not(self) -> None:
        class Flaky(InMemoryMetadata):
            failed: bool = False

            async def search(self, query: SearchQuery) -> list[Title]:
                if not self.failed:
                    self.failed = True
                    raise ProblemError(502, "metadata_unreachable")
                return [found(1, query.title)]

        resolver = TitleResolver(Flaky(), "en")
        await resolver.resolve(WatchedItem("First", "movie"))
        assert (await resolver.resolve(WatchedItem("Second", "movie"))).confident


class TestEngagement:
    """ADR 0013's point 4: the import reproduces the media server's own verdicts."""

    def test_a_series_finished(self) -> None:
        row = watched_title(
            ref=TitleRef("tv", 1),
            source="netflix",
            episodes=15,
            episodes_total=15,
            last_watched_at=NOW,
            now=NOW,
        )
        assert row.state == "watched"
        assert row.progress == 1.0

    def test_a_series_in_progress(self) -> None:
        row = watched_title(
            ref=TitleRef("tv", 1),
            source="netflix",
            episodes=78,
            episodes_total=218,
            last_watched_at=NOW - timedelta(days=3),
            now=NOW,
        )
        assert row.state == "in_progress"

    def test_a_series_sampled_and_dropped(self) -> None:
        row = watched_title(
            ref=TitleRef("tv", 1),
            source="netflix",
            episodes=2,
            episodes_total=60,
            last_watched_at=NOW - timedelta(days=ABANDONED_AFTER_DAYS + 5),
            now=NOW,
        )
        assert row.state == "abandoned"

    def test_the_media_server_ratio_is_the_one_used(self) -> None:
        # 60 % is "mostly watched" there and here; nothing re-decides it.
        row = watched_title(
            ref=TitleRef("tv", 1),
            source="netflix",
            episodes=int(10 * MOSTLY_WATCHED_RATIO),
            episodes_total=10,
            last_watched_at=NOW - timedelta(days=400),
            now=NOW,
        )
        assert row.state == "mostly_watched"

    def test_a_series_with_no_known_total_is_judged_on_recency_alone(self) -> None:
        row = watched_title(
            ref=TitleRef("tv", 1),
            source="netflix",
            episodes=4,
            episodes_total=None,
            last_watched_at=NOW - timedelta(days=2),
            now=NOW,
        )
        assert row.state == "in_progress"
        assert row.progress == 0.0

    def test_a_film_row_is_a_viewing(self) -> None:
        row = watched_title(ref=TitleRef("movie", 1), source="letterboxd", now=NOW)
        assert row.state == "watched"
        assert row.as_engagement() is not None

    def test_a_row_never_becomes_a_vote(self) -> None:
        row = watched_title(ref=TitleRef("movie", 1), source="imdb", rating=9.0, now=NOW)
        engagement = row.as_engagement()
        assert engagement is not None
        assert engagement.item.item_id == "imdb:1"
        assert not hasattr(row, "value")


class TestWholeRun:
    """Parsing, identifying, merging and counting, end to end."""

    async def test_merges_the_episode_rows_of_one_series(self) -> None:
        # An IMDb export rates episodes one at a time. Fifteen of them are one series
        # somebody watched fifteen episodes of, not fifteen titles.
        show = ExternalMatch(TitleRef("tv", 1399), "Game of Thrones", episode=True)
        metadata = InMemoryMetadata(
            external={f"tt{index}": show for index in range(15)},
            episode_counts={TitleRef("tv", 1399): 73},
        )
        outcome = await resolve_import(
            parsed(
                *(
                    WatchedItem(f"Episode {index}", "tv", imdb_id=f"tt{index}", episodes=1)
                    for index in range(15)
                ),
                kind="imdb",
            ),
            metadata,
            language="en",
            now=NOW,
        )
        assert len(outcome.watched) == 1
        assert outcome.watched[0].episodes_played == 15
        assert outcome.watched[0].episodes_total == 73
        assert outcome.watched[0].state == "paused"

    async def test_an_uncertain_row_lands_in_the_queue_with_what_was_offered(self) -> None:
        metadata = InMemoryMetadata(
            search_results={"Michael Jackson": [found(92060, "Michael Jackson: Thriller")]}
        )
        outcome = await resolve_import(
            parsed(WatchedItem("Michael Jackson", "tv", episodes=1)),
            metadata,
            language="fr",
            now=NOW,
        )
        assert outcome.watched == ()
        assert len(outcome.review) == 1
        assert outcome.review[0].query == "Michael Jackson"
        assert outcome.review[0].candidates[0].title == "Michael Jackson: Thriller"
        assert outcome.review[0].episodes == 1

    async def test_the_outcome_carries_what_the_parser_dropped(self) -> None:
        file = ParsedFile(format="netflix", items=(), skipped={"supplemental": 12})
        outcome = await resolve_import(file, InMemoryMetadata(), language="fr", now=NOW)
        assert outcome == ImportOutcome(format="netflix", skipped={"supplemental": 12})

    async def test_a_series_tmdb_will_not_describe_still_gets_a_verdict(self) -> None:
        class Silent(InMemoryMetadata):
            async def details(self, ref: TitleRef, language: str) -> TitleDetails:
                raise ProblemError(502, "metadata_unreachable")

        metadata = Silent(search_results={"Heroes": [found(1639, "Heroes")]})
        outcome = await resolve_import(
            parsed(WatchedItem("Heroes", "tv", episodes=39, last_watched_at=NOW)),
            metadata,
            language="fr",
            now=NOW,
        )
        assert outcome.watched[0].episodes_total is None
        assert outcome.watched[0].state == "in_progress"

    async def test_a_filtered_genre_is_still_part_of_somebody_s_history(self) -> None:
        # A household that stopped wanting horror did not stop having seen it: the
        # filter belongs to the pool, not to the record of what was watched.
        metadata = InMemoryMetadata(search_results={"Saw": [found(1, "Saw", kind="movie")]})
        outcome = await resolve_import(
            parsed(WatchedItem("Saw", "movie")), metadata, language="en", now=NOW
        )
        assert [row.ref for row in outcome.watched] == [TitleRef("movie", 1)]
