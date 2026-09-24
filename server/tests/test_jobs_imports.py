"""Running an import after the upload request has answered, and bounding it.

The interesting cases are the ones an import must not be able to cause: a server full of
them, a task that dies without closing its row, and a restart that leaves a row claiming
to be running for ever.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support import FakeClock
from tindarr.core.errors import ProblemError
from tindarr.jobs.imports import MAX_CONCURRENT_IMPORTS, ImportRunner, close_abandoned_imports
from tindarr.storage import imports as import_repository
from tindarr.storage import users as user_repository
from tindarr.storage.db import write_transaction
from tindarr.storage.imports import ImportRecord
from tindarr.swipe.imports import ParsedFile, WatchedItem

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 24, tzinfo=UTC)
PARSED = ParsedFile(format="netflix", items=(WatchedItem("Heroes", "tv"),))


class SlowService:
    """An import service that never finishes, so a test can watch the bookkeeping."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.failed: list[str] = []

    async def run(self, record: ImportRecord, parsed: ParsedFile) -> None:
        self.started.set()
        await asyncio.Event().wait()

    async def run_failed(self, record: ImportRecord) -> None:
        self.failed.append(record.id)


class ExplodingService(SlowService):
    """One that raises something nobody planned for."""

    async def run(self, record: ImportRecord, parsed: ParsedFile) -> None:
        self.started.set()
        raise RuntimeError("boom")


def record(index: int = 0) -> ImportRecord:
    return ImportRecord(
        id=f"import-{index}",
        user_id="user-1",
        source="netflix",
        status="running",
        rows_read=0,
        rows_skipped="{}",
        matched=0,
        queued=0,
        error_code=None,
        created_at=NOW,
        finished_at=None,
    )


class TestRunner:
    """The tasks, and the cap on them."""

    async def test_it_runs_what_it_is_given(self) -> None:
        service = SlowService()
        runner = ImportRunner(service)  # pyright: ignore[reportArgumentType]
        runner.start()
        runner.submit(record(), PARSED)
        await service.started.wait()
        assert runner.running == 1
        await runner.stop()
        assert runner.running == 0

    async def test_a_server_already_full_of_imports_refuses_another(self) -> None:
        service = SlowService()
        runner = ImportRunner(service)  # pyright: ignore[reportArgumentType]
        assert runner.has_capacity()
        for index in range(MAX_CONCURRENT_IMPORTS):
            runner.submit(record(index), PARSED)
        # The upload path asks this *before* it creates a row; `submit` is the backstop.
        assert not runner.has_capacity()
        with pytest.raises(ProblemError) as failure:
            runner.submit(record(99), PARSED)
        assert failure.value.code == "import_in_progress"
        await runner.stop()
        assert runner.has_capacity()

    async def test_a_task_that_dies_still_closes_its_row(self) -> None:
        service = ExplodingService()
        runner = ImportRunner(service)  # pyright: ignore[reportArgumentType]
        runner.submit(record(), PARSED)
        await service.started.wait()
        await runner.stop()
        assert service.failed == ["import-0"]

    async def test_stopping_twice_is_allowed(self) -> None:
        runner = ImportRunner(SlowService())  # pyright: ignore[reportArgumentType]
        await runner.stop()
        await runner.stop()


class TestRestart:
    """A row still claiming to be running is a status a console would poll for ever."""

    async def test_an_interrupted_import_is_closed_at_startup(self, engine: AsyncEngine) -> None:
        async with write_transaction(engine) as connection:
            user = await user_repository.insert(
                connection,
                user_repository.new_user("a" * 32, "someone", NOW, admin=False, remote=True),
            )
            opened = await import_repository.create(connection, user.id, "netflix", now=NOW)
        assert await close_abandoned_imports(engine, FakeClock(NOW)) == 1
        async with engine.connect() as connection:
            found = await import_repository.get(connection, user.id, opened.id)
        assert found is not None
        assert found.status == "failed"
        assert found.error_code == "interrupted"

    async def test_a_clean_database_has_nothing_to_close(self, engine: AsyncEngine) -> None:
        assert await close_abandoned_imports(engine, FakeClock(NOW)) == 0
