"""The background work that keeps Tindeerr and the media server in step.

Three schedules, each doing one thing (the architecture's "background jobs"):

- **the user sync**, hourly and 60 s after startup: who was removed, disabled or
  demoted on the media server (``tindeerr.auth.sync``);
- **the handle sweep**, every 30 s: expired sign-in handles are dropped, and a Quick
  Connect request the user approved but nobody collected is collected once so that the
  Jellyfin session it opened is closed;
- **the Quick Connect probe**, every five minutes: it refreshes the cached
  ``GET /QuickConnect/Enabled`` that drives ``auth_methods``, so ``GET /server/info``
  never has to call the media server on a client's behalf.

A failure is logged by ``PeriodicJob`` and the schedule is kept: one unreachable media
server never stops the others.
"""

import logging
from datetime import timedelta
from typing import Final

from tindeerr.auth.brokered import QuickConnectFlow
from tindeerr.auth.sync import UserSync
from tindeerr.core.errors import ProblemError
from tindeerr.jobs.purge import PeriodicJob

USER_SYNC_INTERVAL: Final = timedelta(hours=1)
USER_SYNC_FIRST_DELAY: Final = timedelta(seconds=60)
HANDLE_SWEEP_INTERVAL: Final = timedelta(seconds=30)
#: Comfortably shorter than ``QUICK_CONNECT_CACHE``: a probe that ran exactly as often
#: as the cache expires would leave a window before each run where ``auth_methods``
#: drops a Quick Connect that is in fact switched on.
QUICK_CONNECT_INTERVAL: Final = timedelta(minutes=2)
QUICK_CONNECT_FIRST_DELAY: Final = timedelta(seconds=15)

logger = logging.getLogger(__name__)


def user_sync_job(sync: UserSync) -> PeriodicJob:
    """Build the hourly reconciliation with the media server."""

    async def run() -> None:
        result = await sync.run()
        if result.disabled or result.demoted:
            logger.info(
                "media server user sync",
                extra={
                    "users": result.updated,
                    "disabled": result.disabled,
                    "demoted": result.demoted,
                },
            )

    return PeriodicJob("user_sync", run, USER_SYNC_INTERVAL, USER_SYNC_FIRST_DELAY)


def handle_sweep_job(quick_connect: QuickConnectFlow) -> PeriodicJob:
    """Build the sweep that drops expired handles and closes abandoned approvals."""
    return PeriodicJob("handle_sweep", quick_connect.sweep, HANDLE_SWEEP_INTERVAL)


def quick_connect_probe_job(quick_connect: QuickConnectFlow) -> PeriodicJob:
    """Build the probe that keeps ``auth_methods`` honest about Quick Connect."""

    async def run() -> None:
        try:
            await quick_connect.refresh_availability()
        except ProblemError:
            # Not configured, unreachable, or not Jellyfin: the method is simply left
            # out of ``auth_methods`` until the next run succeeds.
            quick_connect.forget_availability()

    return PeriodicJob(
        "quick_connect_probe", run, QUICK_CONNECT_INTERVAL, QUICK_CONNECT_FIRST_DELAY
    )
