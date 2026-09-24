"""Storing what a household has watched, and answering what an import could not settle.

The three properties the whole feature rests on are here as tests rather than as
comments: an import touches one user's rows and nobody else's, nothing it writes is a
vote, and an upload is never kept.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support import FakeClock
from tests.support.evaluation import InMemoryMetadata, title
from tindarr.connectors import ConnectorService
from tindarr.core.errors import ProblemError
from tindarr.ports.factories import ConnectorFactories
from tindarr.ports.history import WatchedTitle
from tindarr.ports.llm import LlmConnection, LlmProvider
from tindarr.ports.metadata import Metadata, RatingsSource, Title
from tindarr.ports.request_backend import RequestBackend, RequestBackendConnection
from tindarr.ports.titles import TitleRef
from tindarr.storage import history as history_repository
from tindarr.storage import imports as import_repository
from tindarr.storage import users as user_repository
from tindarr.storage.db import write_transaction
from tindarr.storage.settings import SettingsStore
from tindarr.storage.tables import metadata as schema
from tindarr.swipe.calibration import GridTick
from tindarr.swipe.watched import GridService, ImportService, household

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 24, tzinfo=UTC)
NETFLIX = (
    b"Title,Date\n"
    b'"Heroes: Saison 1: Genesis","9/10/26"\n'
    b'"Heroes: Saison 1: Don\'t Look Back","9/11/26"\n'
)
#: One row whose episode name exists nowhere but in the file.
NAMED_EPISODE = b'Title,Date\n"Heroes: Saison 1: Genesis","9/10/26"\n'


def _files(root: Path) -> set[str]:
    """Every file under the data directory, by name."""
    return {path.name for path in root.rglob("*") if path.is_file()}


async def _row_counts(engine: AsyncEngine) -> dict[str, int]:
    """How many rows every table holds, so a test can say which ones an action moved."""
    async with engine.connect() as connection:
        return {
            table.name: (
                await connection.execute(select(func.count()).select_from(table))
            ).scalar_one()
            for table in schema.sorted_tables
        }


async def _dump_everything(engine: AsyncEngine) -> str:
    """Every value in every table, as one string. What "it is not stored" is checked on."""
    parts: list[str] = []
    async with engine.connect() as connection:
        for table in schema.sorted_tables:
            for row in (await connection.execute(table.select())).all():
                parts.extend(str(value) for value in row)
    return "\n".join(parts)


def _famous(tmdb_id: int) -> Title:
    """A poster a calibration wall would show."""
    built = title(tmdb_id, f"T{tmdb_id}", kind="movie", year=2015)
    return Title(
        ref=built.ref,
        title=built.title,
        year=built.year,
        vote_count=20_000,
        popularity=50.0,
        genre_ids=built.genre_ids,
    )


def factories(metadata: Metadata) -> ConnectorFactories:
    """Connector factories where only TMDb exists, and it is the one handed in."""

    def build_metadata(api_key: str) -> Metadata:
        return metadata

    def build_ratings(api_key: str) -> RatingsSource:  # pragma: no cover - never built
        raise AssertionError("no OMDb here")

    def build_backend(connection: RequestBackendConnection) -> RequestBackend:  # pragma: no cover
        raise AssertionError("no request backend here")

    def build_llm(connection: LlmConnection) -> LlmProvider:  # pragma: no cover
        raise AssertionError("no AI provider here")

    return ConnectorFactories(build_metadata, build_ratings, build_backend, build_llm)


@pytest.fixture
async def user(engine: AsyncEngine) -> str:
    """One signed-in person, and a second one to prove nothing reaches them."""
    async with write_transaction(engine) as connection:
        for index, media_id in enumerate(("a" * 32, "b" * 32)):
            await user_repository.insert(
                connection,
                user_repository.new_user(media_id, f"user-{index}", NOW, admin=False, remote=True),
            )
        rows = await user_repository.list_all(connection)
    return sorted(row.id for row in rows)[0]


@pytest.fixture
async def other_user(engine: AsyncEngine, user: str) -> str:
    async with engine.connect() as connection:
        rows = await user_repository.list_all(connection)
    return next(row.id for row in rows if row.id != user)


@pytest.fixture
def metadata() -> InMemoryMetadata:
    return InMemoryMetadata(
        search_results={"Heroes": [title(1639, "Heroes", kind="tv", popularity=30.0)]},
        episode_counts={TitleRef("tv", 1639): 78},
    )


@pytest.fixture
async def connectors(settings_store: SettingsStore, metadata: InMemoryMetadata) -> ConnectorService:
    await settings_store.set("tmdb_api_key", "k" * 32)
    return ConnectorService(settings_store, factories(metadata))


@pytest.fixture
def clock_at() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def imports(
    engine: AsyncEngine,
    settings_store: SettingsStore,
    connectors: ConnectorService,
    clock_at: FakeClock,
) -> ImportService:
    return ImportService(engine, settings_store, connectors, clock_at)


async def run_import(service: ImportService, user_id: str, payload: bytes = NETFLIX) -> str:
    """Upload and identify one file, synchronously, as the background task would."""
    record, parsed = await service.start(user_id, payload)
    await service.run(record, parsed)
    return record.id


class TestHistoryRows:
    """The table itself."""

    async def test_a_row_is_written_and_read_back(self, engine: AsyncEngine, user: str) -> None:
        row = WatchedTitle(TitleRef("tv", 1399), "netflix", state="watched", episodes_played=73)
        async with write_transaction(engine) as connection:
            assert await history_repository.record(connection, user, [row], now=NOW) == 1
        async with engine.connect() as connection:
            assert await history_repository.list_for_user(connection, user) == [row]

    async def test_writing_nothing_writes_nothing(self, engine: AsyncEngine, user: str) -> None:
        async with write_transaction(engine) as connection:
            assert await history_repository.record(connection, user, [], now=NOW) == 0

    async def test_a_second_import_replaces_its_own_row(
        self, engine: AsyncEngine, user: str
    ) -> None:
        ref = TitleRef("tv", 1399)
        async with write_transaction(engine) as connection:
            await history_repository.record(
                connection, user, [WatchedTitle(ref, "netflix", episodes_played=2)], now=NOW
            )
            await history_repository.record(
                connection, user, [WatchedTitle(ref, "netflix", episodes_played=9)], now=NOW
            )
        async with engine.connect() as connection:
            rows = await history_repository.list_for_user(connection, user)
        assert [row.episodes_played for row in rows] == [9]

    async def test_two_sources_about_one_title_are_two_rows_and_one_engagement(
        self, engine: AsyncEngine, user: str
    ) -> None:
        ref = TitleRef("tv", 1399)
        async with write_transaction(engine) as connection:
            await history_repository.record(
                connection,
                user,
                [
                    WatchedTitle(ref, "grid"),
                    WatchedTitle(ref, "netflix", state="watched", episodes_played=73),
                ],
                now=NOW,
            )
        async with engine.connect() as connection:
            assert len(await history_repository.list_for_user(connection, user)) == 2
            found = await history_repository.engagements(connection, user)
        # The row that says the most about how far they got is the one the engine sees.
        assert [engagement.state for engagement in found] == ["watched"]

    async def test_a_no_from_the_grid_is_answered_but_not_seen(
        self, engine: AsyncEngine, user: str
    ) -> None:
        ref = TitleRef("movie", 27205)
        async with write_transaction(engine) as connection:
            await history_repository.record(
                connection, user, [WatchedTitle(ref, "grid", seen=False)], now=NOW
            )
        async with engine.connect() as connection:
            assert await history_repository.seen_refs(connection, user) == frozenset()
            assert await history_repository.answered_refs(connection, user) == frozenset({ref})
            assert await history_repository.engagements(connection, user) == ()

    async def test_forgetting_one_source_leaves_the_others(
        self, engine: AsyncEngine, user: str
    ) -> None:
        ref = TitleRef("movie", 1)
        async with write_transaction(engine) as connection:
            await history_repository.record(
                connection,
                user,
                [WatchedTitle(ref, "netflix"), WatchedTitle(ref, "grid")],
                now=NOW,
            )
            assert await history_repository.delete_source(connection, user, "netflix") == 1
        async with engine.connect() as connection:
            rows = await history_repository.list_for_user(connection, user)
        assert [row.source for row in rows] == ["grid"]


class TestOneUserOnly:
    """An import can only ever be wrong about the person who uploaded it."""

    async def test_an_import_writes_nothing_into_another_account(
        self, engine: AsyncEngine, imports: ImportService, user: str, other_user: str
    ) -> None:
        await run_import(imports, user)
        async with engine.connect() as connection:
            assert await history_repository.list_for_user(connection, other_user) == []
            assert await import_repository.list_for_user(connection, other_user) == []

    async def test_another_user_s_import_is_not_found(
        self, imports: ImportService, engine: AsyncEngine, user: str, other_user: str
    ) -> None:
        import_id = await run_import(imports, user)
        async with engine.connect() as connection:
            assert await import_repository.get(connection, other_user, import_id) is None
            assert await import_repository.list_reviews(connection, other_user, import_id) == []

    async def test_another_user_s_review_entry_cannot_be_accepted(
        self, imports: ImportService, engine: AsyncEngine, user: str, other_user: str
    ) -> None:
        await run_import(imports, user, b'Title,Date\n"Mushoku Tensei: Saison 1: A","9/10/26"\n')
        async with engine.connect() as connection:
            entries = await import_repository.list_reviews(
                connection, user, (await import_repository.list_for_user(connection, user))[0].id
            )
        assert entries
        with pytest.raises(ProblemError) as failure:
            await imports.reject(other_user, entries[0].id, entries[0].import_id)
        assert failure.value.code == "not_found"


class TestRunningAnImport:
    """Upload, identify, write."""

    async def test_it_writes_history_and_counts(
        self, engine: AsyncEngine, imports: ImportService, user: str
    ) -> None:
        import_id = await run_import(imports, user)
        async with engine.connect() as connection:
            record = await import_repository.get(connection, user, import_id)
            rows = await history_repository.list_for_user(connection, user)
        assert record is not None
        assert record.status == "complete"
        assert record.source == "netflix"
        assert record.matched == 1
        assert record.rows_read == 1
        assert [(row.ref, row.episodes_played, row.episodes_total) for row in rows] == [
            (TitleRef("tv", 1639), 2, 78)
        ]

    async def test_nothing_it_writes_touches_any_table_but_its_own(
        self, engine: AsyncEngine, imports: ImportService, user: str
    ) -> None:
        # The claim is not "no value looks like a vote" — a type checker proves that.
        # It is that an import writes into three tables and no others, so nothing it
        # does can reach a statistic, a vote count or the deck's calibration progress.
        before = await _row_counts(engine)
        await run_import(imports, user)
        after = await _row_counts(engine)
        touched = {name for name, count in after.items() if count != before.get(name)}
        assert touched == {"watch_history", "imports"}
        async with engine.connect() as connection:
            assert await history_repository.engagements(connection, user)

    async def test_a_file_nobody_can_read_leaves_no_row(
        self, engine: AsyncEngine, imports: ImportService, user: str
    ) -> None:
        with pytest.raises(ProblemError) as failure:
            await imports.start(user, b"who,what\n1,2\n")
        assert failure.value.code == "import_unreadable"
        async with engine.connect() as connection:
            assert await import_repository.list_for_user(connection, user) == []

    async def test_a_server_with_no_capacity_leaves_no_row_behind(
        self, engine: AsyncEngine, imports: ImportService, user: str
    ) -> None:
        # A row opened for work nobody will run stays `running` until the next restart
        # and refuses every later upload by that user, so the capacity is reserved in
        # the same transaction as the one-at-a-time rule.
        with pytest.raises(ProblemError) as failure:
            await imports.start(user, NETFLIX, lambda: False)
        assert failure.value.code == "import_in_progress"
        async with engine.connect() as connection:
            assert await import_repository.list_for_user(connection, user) == []
        # And the user can still upload once the server is free again.
        record, _ = await imports.start(user, NETFLIX, lambda: True)
        assert record.status == "running"

    async def test_only_one_import_at_a_time_per_user(
        self, imports: ImportService, user: str
    ) -> None:
        await imports.start(user, NETFLIX)
        with pytest.raises(ProblemError) as failure:
            await imports.start(user, NETFLIX)
        assert failure.value.code == "import_in_progress"

    async def test_no_tmdb_means_no_import(
        self, engine: AsyncEngine, settings_store: SettingsStore, clock_at: FakeClock, user: str
    ) -> None:
        connectors = ConnectorService(settings_store, factories(InMemoryMetadata()))
        service = ImportService(engine, settings_store, connectors, clock_at)
        with pytest.raises(ProblemError) as failure:
            await service.start(user, NETFLIX)
        assert failure.value.code == "tmdb_not_configured"

    async def test_a_dead_tmdb_closes_the_import_with_a_code_and_no_prose(
        self,
        engine: AsyncEngine,
        settings_store: SettingsStore,
        clock_at: FakeClock,
        user: str,
    ) -> None:
        class Dead(InMemoryMetadata):
            async def search(self, query: object) -> list[Title]:
                raise ProblemError(502, "metadata_unreachable")

        await settings_store.set("tmdb_api_key", "k" * 32)
        service = ImportService(
            engine, settings_store, ConnectorService(settings_store, factories(Dead())), clock_at
        )
        rows = b"Title,Date\n" + b"".join(
            f'"Film {index}","9/10/26"\n'.encode() for index in range(8)
        )
        record, parsed = await service.start(user, rows)
        await service.run(record, parsed)
        async with engine.connect() as connection:
            found = await import_repository.get(connection, user, record.id)
        assert found is not None
        assert found.status == "failed"
        assert found.error_code == "metadata_unreachable"

    async def test_an_unexpected_failure_still_closes_the_row(
        self, engine: AsyncEngine, imports: ImportService, user: str
    ) -> None:
        record, _ = await imports.start(user, NETFLIX)
        await imports.run_failed(record)
        async with engine.connect() as connection:
            found = await import_repository.get(connection, user, record.id)
        assert found is not None
        assert found.error_code == "internal_error"

    async def test_the_file_and_its_name_are_never_written_anywhere(
        self, engine: AsyncEngine, data_dir: Path, imports: ImportService, user: str
    ) -> None:
        # Asserted against every column of every table rather than against the two a
        # bug would have had to pick, and against the data directory: a parse that
        # spooled the upload to disk is exactly what this is here to catch.
        files_before = _files(data_dir)
        await run_import(imports, user, NAMED_EPISODE)
        dumped = await _dump_everything(engine)
        # The series title survives — it is what was identified — but the episode name,
        # which only the file held, does not, and neither does the file name.
        assert "Heroes" in dumped
        assert "Genesis" not in dumped
        assert "history.csv" not in dumped
        assert _files(data_dir) - files_before <= {"tindarr.db-wal", "tindarr.db-shm"}


class TestReviewQueue:
    """What an import refused to guess at, and how it is answered."""

    async def test_an_uncertain_row_is_queued_with_its_candidates(
        self, engine: AsyncEngine, imports: ImportService, user: str
    ) -> None:
        import_id = await run_import(
            imports, user, b'Title,Date\n"Mushoku Tensei: Saison 1: A","9/10/26"\n'
        )
        async with engine.connect() as connection:
            entries = await import_repository.list_reviews(connection, user, import_id)
            record = await import_repository.get(connection, user, import_id)
        assert record is not None
        assert record.queued == 1
        assert entries[0].query == "Mushoku Tensei"
        assert entries[0].hint == "tv"

    async def test_accepting_writes_the_history_row_the_run_would_have(
        self, engine: AsyncEngine, imports: ImportService, metadata: InMemoryMetadata, user: str
    ) -> None:
        metadata.search_results = {
            **metadata.search_results,
            "Mushoku Tensei": [title(94664, "Mushoku Tensei: Jobless Reincarnation", kind="tv")],
        }
        metadata.episode_counts = {TitleRef("tv", 94664): 23}
        import_id = await run_import(
            imports, user, b'Title,Date\n"Mushoku Tensei: Saison 1: A","9/10/26"\n'
        )
        async with engine.connect() as connection:
            entry = (await import_repository.list_reviews(connection, user, import_id))[0]
        row = await imports.accept(user, entry.id, TitleRef("tv", 94664), import_id)
        assert row.source == "netflix"
        assert row.episodes_played == 1
        assert row.episodes_total == 23
        async with engine.connect() as connection:
            assert await history_repository.seen_refs(connection, user) == frozenset(
                {TitleRef("tv", 94664)}
            )

    async def test_a_title_the_entry_never_offered_is_refused(
        self, engine: AsyncEngine, imports: ImportService, metadata: InMemoryMetadata, user: str
    ) -> None:
        metadata.search_results = {
            **metadata.search_results,
            "Mushoku Tensei": [title(94664, "Mushoku Tensei: Jobless Reincarnation", kind="tv")],
        }
        import_id = await run_import(
            imports, user, b'Title,Date\n"Mushoku Tensei: Saison 1: A","9/10/26"\n'
        )
        async with engine.connect() as connection:
            entry = (await import_repository.list_reviews(connection, user, import_id))[0]
        with pytest.raises(ProblemError) as failure:
            await imports.accept(user, entry.id, TitleRef("movie", 999), import_id)
        assert failure.value.code == "validation_error"

    async def test_rejecting_writes_nothing(
        self, engine: AsyncEngine, imports: ImportService, user: str
    ) -> None:
        import_id = await run_import(
            imports, user, b'Title,Date\n"Mushoku Tensei: Saison 1: A","9/10/26"\n'
        )
        async with engine.connect() as connection:
            entry = (await import_repository.list_reviews(connection, user, import_id))[0]
        await imports.reject(user, entry.id, import_id)
        async with engine.connect() as connection:
            assert await history_repository.list_for_user(connection, user) == []
            assert await import_repository.count_pending(connection, user, import_id) == 0

    async def test_a_question_is_answered_once(
        self, engine: AsyncEngine, imports: ImportService, user: str
    ) -> None:
        import_id = await run_import(
            imports, user, b'Title,Date\n"Mushoku Tensei: Saison 1: A","9/10/26"\n'
        )
        async with engine.connect() as connection:
            entry = (await import_repository.list_reviews(connection, user, import_id))[0]
        await imports.reject(user, entry.id, import_id)
        with pytest.raises(ProblemError):
            await imports.reject(user, entry.id, import_id)

    async def test_an_entry_that_does_not_exist(self, imports: ImportService, user: str) -> None:
        with pytest.raises(ProblemError) as failure:
            await imports.reject(user, "nope", "nope")
        assert failure.value.code == "not_found"


class TestForgetting:
    """Undoing an import, which is also how somebody unsays a file."""

    async def test_it_removes_the_rows_that_source_wrote(
        self, engine: AsyncEngine, imports: ImportService, user: str
    ) -> None:
        import_id = await run_import(imports, user)
        await imports.forget(user, import_id)
        async with engine.connect() as connection:
            assert await history_repository.list_for_user(connection, user) == []
            assert await import_repository.get(connection, user, import_id) is None

    async def test_it_leaves_a_second_import_of_the_same_format_standing(
        self, engine: AsyncEngine, imports: ImportService, metadata: InMemoryMetadata, user: str
    ) -> None:
        # Two Netflix exports. Deleting the first must not take the second's work with
        # it, or the counts the console shows for the one that remains become a lie.
        metadata.search_results = {
            **metadata.search_results,
            "Dark": [title(70523, "Dark", kind="tv", popularity=30.0)],
        }
        metadata.episode_counts = {**metadata.episode_counts, TitleRef("tv", 70523): 26}
        first = await run_import(imports, user)
        second = await run_import(
            imports, user, b'Title,Date\n"Dark: Saison 1: Geheimnisse","9/10/26"\n'
        )
        await imports.forget(user, first)
        async with engine.connect() as connection:
            rows = await history_repository.list_for_user(connection, user)
            assert await import_repository.get(connection, user, second) is not None
        assert [row.ref for row in rows] == [TitleRef("tv", 70523)]

    async def test_it_leaves_the_calibration_answers_standing(
        self, engine: AsyncEngine, imports: ImportService, user: str
    ) -> None:
        async with write_transaction(engine) as connection:
            await history_repository.record(
                connection, user, [WatchedTitle(TitleRef("movie", 27205), "grid")], now=NOW
            )
        await imports.forget(user, await run_import(imports, user))
        async with engine.connect() as connection:
            rows = await history_repository.list_for_user(connection, user)
        assert [row.source for row in rows] == ["grid"]

    async def test_an_import_that_is_not_theirs(self, imports: ImportService, user: str) -> None:
        with pytest.raises(ProblemError) as failure:
            await imports.forget(user, "nope")
        assert failure.value.code == "not_found"


class TestGrid:
    """The wall, wired to the household's settings and to what it already knows."""

    @pytest.fixture
    def grid(
        self,
        engine: AsyncEngine,
        settings_store: SettingsStore,
        connectors: ConnectorService,
        clock_at: FakeClock,
    ) -> GridService:
        return GridService(engine, settings_store, connectors, clock_at)

    async def test_a_tick_becomes_history_and_excludes_the_title(
        self, engine: AsyncEngine, grid: GridService, user: str
    ) -> None:
        ref = TitleRef("movie", 27205)
        assert await grid.submit(user, [GridTick(ref, seen=True)]) == 1
        async with engine.connect() as connection:
            assert await history_repository.seen_refs(connection, user) == frozenset({ref})

    async def test_a_wall_never_asks_about_a_title_that_was_answered_no(
        self, grid: GridService, metadata: InMemoryMetadata, user: str
    ) -> None:
        asked = TitleRef("movie", 27205)
        metadata.pages = {("movie", 1): [_famous(asked.tmdb_id)], ("tv", 1): []}
        before = await grid.build(user, page=1, size=10)
        assert asked in {found.ref for found in before}
        await grid.submit(user, [GridTick(asked, seen=False)])
        after = await grid.build(user, page=1, size=10)
        assert asked not in {found.ref for found in after}

    async def test_no_tmdb_means_no_wall(
        self, engine: AsyncEngine, settings_store: SettingsStore, clock_at: FakeClock, user: str
    ) -> None:
        service = GridService(
            engine,
            settings_store,
            ConnectorService(settings_store, factories(InMemoryMetadata())),
            clock_at,
        )
        with pytest.raises(ProblemError) as failure:
            await service.build(user, page=1, size=10)
        assert failure.value.code == "tmdb_not_configured"


class TestHousehold:
    """The settings, read as the engine wants them."""

    async def test_the_defaults(self, settings_store: SettingsStore) -> None:
        place = await household(settings_store)
        assert (place.language, place.region) == ("en", "US")
        assert place.filters.exclude_adult

    async def test_what_the_administrator_set(self, settings_store: SettingsStore) -> None:
        await settings_store.set("language", "fr")
        await settings_store.set("streaming_region", "FR")
        await settings_store.set(
            "content_filters", {"exclude_adult": False, "excluded_genres": ["Horror"]}
        )
        place = await household(settings_store)
        assert (place.language, place.region) == ("fr", "FR")
        assert place.filters.excluded_genres == frozenset({"Horror"})
        assert not place.filters.exclude_adult
