"""Running an upload's TMDb lookups after the request that carried it has answered.

Identifying seventy titles takes tens of seconds and a hundred TMDb requests. Doing it
inside the upload request would mean a browser holding a connection open for a minute
and an import lost to any reload; doing it in the persisted job system 4.5 brings would
mean building that system now. So this is the smallest thing that is correct: one
asyncio task per import, owned by the application and cancelled with it, with the state
that matters in the ``imports`` row rather than in the task.

**It is bounded twice.** One import at a time per user, enforced in the database by
``running_for_user`` before a row is created; and a global cap here, because a household
is several users and a server is not a queue somebody else fills. Past either, the
upload is refused with ``import_in_progress`` rather than accepted and starved.

**A crash loses the work, not the truth.** An import interrupted by a restart stays
``running`` in the database and is closed as failed at the next startup, so nothing is
left claiming to be in progress for ever and nothing half-written survives: the history
rows and the review queue are written in one transaction at the end.
"""

import asyncio
import contextlib
import logging
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr.core.clock import Clock
from tindarr.storage import imports as import_repository
from tindarr.storage.db import write_transaction
from tindarr.storage.imports import ImportRecord
from tindarr.swipe.imports import ParsedFile
from tindarr.swipe.watched import ImportService, import_in_progress

__all__ = ["MAX_CONCURRENT_IMPORTS", "ImportRunner", "close_abandoned_imports"]

logger = logging.getLogger(__name__)

#: How many imports the whole server runs at once. Each is a few hundred TMDb requests
#: and a few hundred kilobytes; a household is not a work queue.
MAX_CONCURRENT_IMPORTS: Final = 2


class ImportRunner:
    """Owns the tasks that run imports, and stops them when the application does."""

    name = "imports"

    def __init__(self, service: ImportService) -> None:
        self._service = service
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        """Nothing to schedule: this job only ever runs work an upload handed it."""

    @property
    def running(self) -> int:
        """How many imports are being identified right now."""
        return len(self._tasks)

    def submit(self, record: ImportRecord, parsed: ParsedFile) -> None:
        """Run one import in the background, or refuse because the server is busy."""
        if len(self._tasks) >= MAX_CONCURRENT_IMPORTS:
            raise import_in_progress()
        task = asyncio.create_task(self._run(record, parsed), name="tindarr.jobs.import")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, record: ImportRecord, parsed: ParsedFile) -> None:
        try:
            await self._service.run(record, parsed)
        except asyncio.CancelledError:
            raise
        except Exception:
            # An import must never take the server with it, and the row must never be
            # left claiming to be running. The reason is logged, not shown.
            logger.exception("an import failed unexpectedly")
            with contextlib.suppress(Exception):
                await self._service.run_failed(record)

    async def drain(self) -> None:
        """Wait for the running imports to finish rather than cancelling them.

        Shutdown cancels instead: an upload nobody is waiting for is not worth delaying
        a stop for, and the row it leaves behind is closed at the next startup. This is
        here for the tests, which need the background half of an upload to have happened
        before they can assert anything about it.
        """
        for task in list(self._tasks):
            with contextlib.suppress(Exception):
                await task

    async def stop(self) -> None:
        """Cancel every running import and wait for them."""
        tasks, self._tasks = set(self._tasks), set()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def close_abandoned_imports(engine: AsyncEngine, clock: Clock) -> int:
    """Close every import left ``running`` by a restart; return how many.

    Called once at startup, before anything is served. An import's work lives in a task
    and the task died with the process, so a row still claiming to be running is a lie
    the console would poll for ever.
    """
    now = clock.now()
    async with write_transaction(engine) as connection:
        abandoned = await import_repository.list_running(connection)
        for record in abandoned:
            await import_repository.finish(
                connection, record.id, status="failed", now=now, error_code="interrupted"
            )
    if abandoned:
        logger.info("imports interrupted by a restart were closed", extra={"count": len(abandoned)})
    return len(abandoned)
