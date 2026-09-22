"""Background jobs: the schedule they share and the daily purge."""

import asyncio
import logging
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support import FakeClock
from tindarr.auth.sessions import SessionService
from tindarr.jobs.purge import PURGE_FIRST_DELAY, PURGE_INTERVAL, PeriodicJob, purge_job
from tindarr.storage import users as user_repository
from tindarr.storage.db import write_transaction
from tindarr.storage.sessions import Device

pytestmark = pytest.mark.anyio


async def test_a_job_runs_on_its_schedule() -> None:
    runs = asyncio.Event()
    count = [0]

    async def work() -> None:
        count[0] += 1
        runs.set()

    job = PeriodicJob("test", work, timedelta(milliseconds=1))
    job.start()
    job.start()  # starting twice is a no-op
    await asyncio.wait_for(runs.wait(), timeout=2)
    await job.stop()
    await job.stop()  # stopping twice is a no-op
    assert count[0] >= 1


async def test_a_failing_job_is_logged_and_keeps_its_schedule(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def boom() -> None:
        msg = "the media server is down"
        raise RuntimeError(msg)

    job = PeriodicJob("test", boom, timedelta(seconds=60))
    with caplog.at_level(logging.ERROR):
        await job.run_once()
        await job.run_once()
    failures = [record for record in caplog.records if record.__dict__.get("job") == "test"]
    assert len(failures) == 2
    assert all(record.exc_info for record in failures)


async def test_the_purge_job_deletes_what_the_service_finds(
    engine: AsyncEngine,
    sessions: SessionService,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with write_transaction(engine) as connection:
        user = await user_repository.insert(
            connection,
            user_repository.new_user("media-1", "Alex", clock.now(), admin=True, remote=True),
        )
        grant = await sessions.open_mobile_session(connection, user, Device("Pixel 9", "android"))
    await sessions.revoke(grant.session.id, "logout")
    clock.advance(timedelta(days=31))

    job = purge_job(sessions)
    with caplog.at_level(logging.INFO):
        await job.run_once()

    assert "purged old rows" in caplog.text
    assert (await sessions.purge()).sessions == 0
    assert timedelta(days=1) == PURGE_INTERVAL
    assert timedelta(minutes=1) == PURGE_FIRST_DELAY


async def test_a_quiet_purge_says_nothing(
    sessions: SessionService, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        await purge_job(sessions).run_once()
    assert "purged old rows" not in caplog.text
