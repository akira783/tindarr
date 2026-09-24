"""The ``jobs`` table: background work whose state outlives the task running it.

A generation lives in an asyncio task, and a task dies with the process. Everything that
has to survive that is here: what is running, what it produced, and why it stopped. The
deck endpoint polls this table, the one-at-a-time rule is enforced against it, and a
restart closes whatever it finds still claiming to run.

**The one-at-a-time rule is a read followed by a write, and it is only correct inside a
write transaction.** ``claim`` checks for an active job of the same kind for the same
user and inserts in the same statement sequence; the caller must hold SQLite's write
lock (``tindarr.storage.db.write_transaction``, which begins ``IMMEDIATE``) so no second
request can slip between the two. That is the same shape the import runner uses, and it
is the only thing standing between a user with two phones and two paid generations.

**A failure is news exactly once.** The contract's deck answers ``502`` for a generation
that failed, and then must stop: a deck that reported the same dead provider for ever
could never recover on its own. ``reported_at`` is that latch.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal, get_args

from sqlalchemy import Row, delete, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from tindarr.storage.ids import new_id
from tindarr.storage.tables import jobs

__all__ = [
    "ACTIVE_STATUSES",
    "JOB_KINDS",
    "JOB_MAX_RUNTIME",
    "JOB_RETENTION",
    "JobKind",
    "JobRecord",
    "JobStatus",
    "active",
    "active_users",
    "claim",
    "close_abandoned",
    "close_stuck",
    "fail",
    "finish",
    "get",
    "last_finished",
    "mark_reported",
    "purge",
    "start",
]

#: What a job can be. Both are per user and both are one at a time.
type JobKind = Literal["batch", "profile"]
type JobStatus = Literal["pending", "running", "done", "failed"]

JOB_KINDS: Final[tuple[JobKind, ...]] = get_args(JobKind.__value__)
#: A job in one of these is one somebody is waiting for.
ACTIVE_STATUSES: Final[tuple[JobStatus, ...]] = ("pending", "running")
#: How long a finished job row is kept, in days. Long enough for a console to explain
#: last night's failure, short enough that the table stays a queue and not a log.
JOB_RETENTION: Final = 14
#: How long a job may claim to be running before the purge stops believing it.
#:
#: A job's task dies with the process, and ``close_abandoned`` catches that at startup.
#: This catches the other shape: a task that is still alive and stuck — an HTTP call
#: that never times out, a provider holding a connection open. Without it, one hung call
#: is a permanent ``202`` for that user until somebody restarts the server. Generous,
#: because a real batch is a TMDb pool and a model call and may legitimately take a
#: minute on a slow reasoning model.
JOB_MAX_RUNTIME: Final = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One row of ``jobs``."""

    id: str
    kind: str
    user_id: str
    status: str
    error_code: str | None
    result_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    reported_at: datetime | None

    @property
    def active(self) -> bool:
        """Whether somebody is still waiting for this job."""
        return self.status in ACTIVE_STATUSES


async def claim(
    connection: AsyncConnection, user_id: str, kind: JobKind, *, now: datetime
) -> JobRecord | None:
    """Open a job for this user, or return ``None`` because one is already running.

    Must be called inside a write transaction: the check and the insert are one
    decision, and SQLite's write lock is what makes them one statement's worth of
    atomicity (see the module docstring).
    """
    if await active(connection, user_id, kind) is not None:
        return None
    job_id = new_id()
    await connection.execute(
        jobs.insert().values(
            id=job_id, kind=kind, user_id=user_id, status="pending", created_at=now
        )
    )
    found = await get(connection, job_id)
    if found is None:  # pragma: no cover - the row was inserted in this transaction
        msg = "the job that was just inserted is not there"
        raise LookupError(msg)
    return found


async def get(connection: AsyncConnection, job_id: str) -> JobRecord | None:
    """Return one job by id, whoever owns it. Callers that answer a user scope first."""
    row = (await connection.execute(jobs.select().where(jobs.c.id == job_id))).first()
    return None if row is None else _to_job(row)


async def active(connection: AsyncConnection, user_id: str, kind: JobKind) -> JobRecord | None:
    """Return this user's pending or running job of that kind, if there is one."""
    statement = (
        jobs.select()
        .where(jobs.c.user_id == user_id)
        .where(jobs.c.kind == kind)
        .where(jobs.c.status.in_(ACTIVE_STATUSES))
        .order_by(jobs.c.created_at)
        .limit(1)
    )
    row = (await connection.execute(statement)).first()
    return None if row is None else _to_job(row)


async def last_finished(
    connection: AsyncConnection, user_id: str, kind: JobKind
) -> JobRecord | None:
    """Return this user's most recently finished job of that kind, done or failed."""
    statement = (
        jobs.select()
        .where(jobs.c.user_id == user_id)
        .where(jobs.c.kind == kind)
        .where(jobs.c.status.in_(("done", "failed")))
        .order_by(jobs.c.finished_at.desc(), jobs.c.created_at.desc())
        .limit(1)
    )
    row = (await connection.execute(statement)).first()
    return None if row is None else _to_job(row)


async def start(connection: AsyncConnection, job_id: str, *, now: datetime) -> None:
    """Mark a claimed job as running."""
    await connection.execute(
        update(jobs)
        .where(jobs.c.id == job_id)
        .where(jobs.c.status == "pending")
        .values(status="running", started_at=now)
    )


async def finish(
    connection: AsyncConnection, job_id: str, *, result_id: str | None, now: datetime
) -> None:
    """Mark a job as done, with whatever it produced."""
    await connection.execute(
        update(jobs)
        .where(jobs.c.id == job_id)
        .values(status="done", result_id=result_id, error_code=None, finished_at=now)
    )


async def fail(connection: AsyncConnection, job_id: str, *, code: str, now: datetime) -> None:
    """Mark a job as failed, with the problem code a client will be told once."""
    await connection.execute(
        update(jobs)
        .where(jobs.c.id == job_id)
        .values(status="failed", error_code=code, finished_at=now)
    )


async def mark_reported(connection: AsyncConnection, job_id: str, *, now: datetime) -> bool:
    """Latch a failure as told; return ``False`` if somebody else told it first.

    The ``reported_at IS NULL`` in the update is the whole mechanism: two polls arriving
    together produce exactly one ``502``, and the second sees the deck it was going to
    see anyway.
    """
    result = await connection.execute(
        update(jobs)
        .where(jobs.c.id == job_id)
        .where(jobs.c.reported_at.is_(None))
        .values(reported_at=now)
    )
    return result.rowcount > 0


async def close_abandoned(connection: AsyncConnection, *, now: datetime) -> list[JobRecord]:
    """Fail every job a restart left behind, and return them.

    A job's work lives in a task and the task died with the process. A row still
    claiming to be running is a status the deck would poll for ever, and — worse — a
    lock on that user's next generation that nothing would ever release.
    """
    statement = jobs.select().where(jobs.c.status.in_(ACTIVE_STATUSES))
    abandoned = _to_jobs(await connection.execute(statement))
    for job in abandoned:
        await fail(connection, job.id, code="interrupted", now=now)
    return abandoned


async def purge(connection: AsyncConnection, *, now: datetime) -> int:
    """Delete finished job rows older than the retention window; return how many."""
    result = await connection.execute(
        delete(jobs)
        .where(jobs.c.status.in_(("done", "failed")))
        .where(jobs.c.finished_at < now - timedelta(days=JOB_RETENTION))
    )
    return result.rowcount


async def close_stuck(connection: AsyncConnection, *, now: datetime) -> list[JobRecord]:
    """Fail every job that has claimed to be running for too long; return them.

    The companion of ``close_abandoned``, for the task that did not die but did not
    finish either. Both release the same thing: that user's next generation.
    """
    statement = (
        jobs.select()
        .where(jobs.c.status.in_(ACTIVE_STATUSES))
        .where(jobs.c.created_at < now - JOB_MAX_RUNTIME)
    )
    stuck = _to_jobs(await connection.execute(statement))
    for job in stuck:
        await fail(connection, job.id, code="interrupted", now=now)
    return stuck


async def active_users(connection: AsyncConnection, kind: JobKind) -> frozenset[str]:
    """Which users currently have a job of that kind in flight."""
    statement = (
        select(jobs.c.user_id).where(jobs.c.kind == kind).where(jobs.c.status.in_(ACTIVE_STATUSES))
    )
    return frozenset(str(row.user_id) for row in (await connection.execute(statement)).all())


def _to_jobs(result: Iterable[Row[tuple[Any, ...]]]) -> list[JobRecord]:
    return [_to_job(row) for row in result]


def _to_job(row: Row[tuple[Any, ...]]) -> JobRecord:
    return JobRecord(
        id=row.id,
        kind=row.kind,
        user_id=row.user_id,
        status=row.status,
        error_code=row.error_code,
        result_id=row.result_id,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        reported_at=row.reported_at,
    )
