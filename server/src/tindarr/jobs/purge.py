"""Background jobs that run inside the API process.

``PeriodicJob`` is the shape they all take (step 2b's user sync and Quick Connect sweep,
step 4's warm-up): a coroutine run a first time after ``first_delay``, then every
``interval``. A failure is logged and the job keeps its schedule, so one unreachable
media server never stops the others.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Final

from tindarr.auth.sessions import SessionService

#: The purge runs daily, a minute after startup.
PURGE_INTERVAL: Final = timedelta(days=1)
PURGE_FIRST_DELAY: Final = timedelta(minutes=1)

logger = logging.getLogger(__name__)


class PeriodicJob:
    """Runs one coroutine on a schedule until it is stopped."""

    def __init__(
        self,
        name: str,
        run: Callable[[], Awaitable[object]],
        interval: timedelta,
        first_delay: timedelta = timedelta(0),
    ) -> None:
        self.name = name
        self._run = run
        self._interval = interval.total_seconds()
        self._first_delay = first_delay.total_seconds()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the job as a background task."""
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name=f"tindarr.jobs.{self.name}")

    async def stop(self) -> None:
        """Cancel the job and wait for it to finish."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def run_once(self) -> None:
        """Run the job now, logging a failure instead of raising."""
        try:
            await self._run()
        except Exception:
            logger.exception("background job failed", extra={"job": self.name})

    async def _loop(self) -> None:
        await asyncio.sleep(self._first_delay)
        while True:
            await self.run_once()
            await asyncio.sleep(self._interval)


def purge_job(sessions: SessionService) -> PeriodicJob:
    """Build the daily purge: old revoked sessions, refresh tokens and pairings."""

    async def run() -> None:
        result = await sessions.purge()
        if result.sessions or result.pairings:
            logger.info(
                "purged old rows",
                extra={"sessions": result.sessions, "pairings": result.pairings},
            )

    return PeriodicJob("purge", run, PURGE_INTERVAL, PURGE_FIRST_DELAY)
