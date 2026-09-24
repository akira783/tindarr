"""Where a generation and a profile rewrite actually run, and what runs them unasked.

Three schedules and two runners (docs/architecture.md, "Background jobs"):

- the **batch runner**, which owns the asyncio task a claimed generation runs in. The
  deck decides that a batch is needed and who pays for it; this only decides whether the
  *server* has room for another one right now;
- the **profile runner**, the same shape for a rewrite;
- the **warm-up**, 30 s after startup and every six hours, which pays for a batch for
  everybody who has used the server in the last fortnight so that opening the app is a
  deck rather than a spinner;
- the **daily purge** of cards nobody voted on, finished job rows and stale receipts.

The pattern is the import runner's, for the same reasons and with the same two bounds: a
job is claimed in the database before a task exists for it, and the number of tasks is
capped for the whole server, because a household is several people and a generation is a
TMDb pool and a model call. A job whose task died with the process is closed at the next
startup (``close_abandoned_jobs``) — otherwise the row would hold that user's slot until
somebody noticed.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Coroutine
from datetime import timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr.core.clock import Clock
from tindarr.core.errors import ProblemError
from tindarr.jobs.purge import PeriodicJob
from tindarr.storage import batches as batch_repository
from tindarr.storage import jobs as job_repository
from tindarr.storage import profiles as profile_repository
from tindarr.storage import users as user_repository
from tindarr.storage import votes as vote_repository
from tindarr.storage.db import write_transaction
from tindarr.storage.usage import DailyLimitReachedError
from tindarr.storage.users import User
from tindarr.swipe.deck import DeckService
from tindarr.swipe.engine import DECK_LOW_WATER, DeckRequest, SwipeEngine
from tindarr.swipe.generation import BatchGenerator
from tindarr.swipe.profile import ProfileService

__all__ = [
    "MAX_CONCURRENT_GENERATIONS",
    "WARM_UP_ACTIVE_DAYS",
    "WARM_UP_FIRST_DELAY",
    "WARM_UP_INTERVAL",
    "BatchRunner",
    "ProfileRunner",
    "close_abandoned_jobs",
    "swipe_purge_job",
    "warm_up_job",
]

logger = logging.getLogger(__name__)

#: How many generations the whole server runs at once. Each is a TMDb pool and a model
#: call; a household is not a work queue, and the AI provider has its own rate limit.
MAX_CONCURRENT_GENERATIONS: Final = 2
#: The same bound for profile rewrites, which are one model call each and never urgent.
MAX_CONCURRENT_REWRITES: Final = 1
#: The warm-up's schedule (docs/architecture.md).
WARM_UP_INTERVAL: Final = timedelta(hours=6)
WARM_UP_FIRST_DELAY: Final = timedelta(seconds=30)
#: Who the warm-up pays for: anybody who used the server in the last fortnight. Longer
#: and it is buying batches for people who have stopped; shorter and somebody who was
#: away for a week comes back to a spinner.
WARM_UP_ACTIVE_DAYS: Final = 14
#: The swipe purge runs daily, a few minutes after startup — after the session purge, so
#: two sweeps do not take the write lock at the same moment on a cold start.
SWIPE_PURGE_INTERVAL: Final = timedelta(days=1)
SWIPE_PURGE_FIRST_DELAY: Final = timedelta(minutes=2)


class _Runner:
    """Owns the tasks one kind of background work runs in, and stops them with the app."""

    def __init__(self, name: str, limit: int) -> None:
        self.name = name
        self._limit = limit
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        """Nothing to schedule: this job only ever runs work somebody handed it."""

    @property
    def running(self) -> int:
        """How many of these are in flight right now."""
        return len(self._tasks)

    def has_capacity(self) -> bool:
        """Whether the server could run another one right now.

        Asked **before** the job row is claimed, for the reason the import runner
        learned: a row opened for work nobody will run holds that user's slot until the
        next restart.
        """
        return len(self._tasks) < self._limit

    def _run(
        self, work: Coroutine[Any, Any, object], on_failure: Callable[[], Awaitable[object]]
    ) -> None:
        task = asyncio.create_task(
            self._guarded(work, on_failure), name=f"tindarr.jobs.{self.name}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(
        self, work: Coroutine[Any, Any, object], on_failure: Callable[[], Awaitable[object]]
    ) -> None:
        try:
            await work
        except asyncio.CancelledError:
            raise
        except Exception:
            # Background work must never take the server with it, and must never leave
            # a row claiming to run: that row is somebody's next generation.
            logger.exception("background swipe work failed unexpectedly")
            with contextlib.suppress(Exception):
                await on_failure()

    async def drain(self) -> None:
        """Wait for what is running rather than cancelling it (the tests need this)."""
        for task in list(self._tasks):
            with contextlib.suppress(Exception):
                await task

    async def stop(self) -> None:
        """Cancel everything running and wait for it."""
        tasks, self._tasks = set(self._tasks), set()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


class BatchRunner(_Runner):
    """Runs claimed generations. The deck claims; this only owns the task."""

    def __init__(self, generator: BatchGenerator) -> None:
        super().__init__("generation", MAX_CONCURRENT_GENERATIONS)
        self._generator = generator

    def submit(self, user: User, request: DeckRequest, job_id: str) -> None:
        """Run one claimed generation in the background."""
        self._run(
            self._generator.run(user, request, job_id),
            lambda: self._generator.fail(user, job_id, "internal_error"),
        )


class ProfileRunner(_Runner):
    """Runs profile rewrites, and is also what debounces them: one per user at a time."""

    def __init__(self, profiles: ProfileService) -> None:
        super().__init__("profile", MAX_CONCURRENT_REWRITES)
        self._profiles = profiles

    async def request_rewrite(self, user: User) -> bool:
        """Claim and run one rewrite for this user; ``False`` when it cannot be run.

        Cannot be run covers all three of "one is already going", "the server is busy"
        and "their daily budget is spent". None of them is worth an error: a rewrite is
        something the deck does for somebody, not something they asked for.
        """
        if not self.has_capacity():
            return False
        try:
            job_id = await self._profiles.claim(user)
        except ProblemError as failure:
            logger.info("a profile rewrite could not start", extra={"reason": failure.code})
            return False
        if job_id is None:
            return False
        self._run(
            self._profiles.rewrite(user, job_id),
            lambda: self._profiles.fail(user, job_id, "internal_error"),
        )
        return True


def warm_up_job(  # noqa: PLR0913 - one argument per collaborator the sweep needs
    engine: AsyncEngine,
    *,
    swipe: SwipeEngine,
    deck: DeckService,
    generator: BatchGenerator,
    runner: BatchRunner,
    clock: Clock,
) -> PeriodicJob:
    """Build the warm-up: a batch ready for everybody who has been here lately.

    It is deliberately the cheapest possible version of itself. It pays only for people
    who used the server in the last fortnight, only when they have no batch waiting and
    too few cards left, and it stops as soon as the server is busy — a household that
    has all come home at once is better served by their own polls than by a sweep
    holding every slot.
    """

    async def run() -> None:
        place = await swipe.household()
        if not place.warm_up_enabled:
            return
        prepared = 0
        for user in await _recent_users(engine, clock):
            if not runner.has_capacity():
                break
            if await _warmed(engine, deck, user, clock):
                continue
            if await _prepare(deck, generator, runner, user):
                prepared += 1
        if prepared:
            logger.info("the warm-up prepared batches", extra={"users": prepared})

    return PeriodicJob("swipe_warm_up", run, WARM_UP_INTERVAL, WARM_UP_FIRST_DELAY)


async def _recent_users(engine: AsyncEngine, clock: Clock) -> list[User]:
    """Everybody enabled who was seen inside the warm-up window."""
    since = clock.now() - timedelta(days=WARM_UP_ACTIVE_DAYS)
    async with engine.connect() as connection:
        everyone = await user_repository.list_all(connection)
    return [
        user
        for user in everyone
        if user.enabled and user.last_seen_at is not None and user.last_seen_at >= since
    ]


async def _warmed(engine: AsyncEngine, deck: DeckService, user: User, clock: Clock) -> bool:
    """Whether this user already has something to swipe, or something being built."""
    preferences = await deck.preferences(user.id)
    now = clock.now()
    async with engine.connect() as connection:
        if await job_repository.active(connection, user.id, "batch") is not None:
            return True
        if await batch_repository.ready_batch(connection, user.id, preferences.media_filter):
            return True
        pending = await batch_repository.count_pending(
            connection, user.id, preferences.media_filter, now=now
        )
    return pending >= DECK_LOW_WATER


async def _prepare(
    deck: DeckService, generator: BatchGenerator, runner: BatchRunner, user: User
) -> bool:
    """Claim and run one warm-up generation, swallowing every reason it cannot happen."""
    preferences = await deck.preferences(user.id)
    request = DeckRequest(
        media_filter=preferences.media_filter, novelty=preferences.novelty, mood=None
    )
    try:
        job_id = await generator.claim(user, request)
    except (ProblemError, DailyLimitReachedError) as failure:
        logger.info("the warm-up skipped a user", extra={"reason": _reason(failure)})
        return False
    if job_id is None:
        return False
    runner.submit(user, request, job_id)
    return True


def _reason(failure: Exception) -> str:
    return failure.code if isinstance(failure, ProblemError) else "daily_limit_reached"


def swipe_purge_job(engine: AsyncEngine, clock: Clock) -> PeriodicJob:
    """Build the daily sweep: unvoted expired cards, old job rows, stale receipts."""

    async def run() -> None:
        now = clock.now()
        async with write_transaction(engine) as connection:
            cards = await batch_repository.purge(connection, now=now)
            finished = await job_repository.purge(connection, now=now)
            receipts = await vote_repository.purge_receipts(connection, now=now)
        if cards or finished or receipts:
            logger.info(
                "purged old swipe rows",
                extra={"cards": cards, "jobs": finished, "receipts": receipts},
            )

    return PeriodicJob("swipe_purge", run, SWIPE_PURGE_INTERVAL, SWIPE_PURGE_FIRST_DELAY)


async def close_abandoned_jobs(engine: AsyncEngine, clock: Clock) -> int:
    """Fail every job a restart left behind; return how many.

    Called once at startup, before anything is served. A generation lives in a task and
    the task died with the process: a row still claiming to run is a status the deck
    would poll for ever **and** a lock on that user's next batch that nothing releases.
    The failure is recorded as ``interrupted``, which the deck deliberately does not
    report to anybody (``tindarr.swipe.deck``) — it starts a new batch instead.
    """
    async with write_transaction(engine) as connection:
        abandoned = await job_repository.close_abandoned(connection, now=clock.now())
        for job in abandoned:
            if job.kind == "profile":
                await profile_repository.clear_refresh_error(connection, job.user_id)
    if abandoned:
        logger.info("jobs interrupted by a restart were closed", extra={"count": len(abandoned)})
    return len(abandoned)
