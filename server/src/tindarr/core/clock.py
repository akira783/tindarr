"""Time, behind a small interface so that lifetimes and rate limits can be tested.

Every component that decides something from the current time (session lifetimes, token
expiry, rate-limit windows, the global slowdown) takes a ``Clock`` instead of calling
``datetime.now`` or ``time.monotonic`` itself. Production code uses ``SystemClock``;
tests use a fake that only moves when told to.
"""

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """A source of wall-clock time, monotonic time and sleeping."""

    def now(self) -> datetime:
        """Return the current time, timezone-aware, in UTC."""
        ...

    def monotonic(self) -> float:
        """Return seconds from an arbitrary origin, never going backwards (rate-limit windows)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait ``seconds`` (the global slowdown)."""
        ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        """Return ``datetime.now(UTC)``."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """Return ``time.monotonic()``."""
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """Sleep with ``asyncio.sleep``."""
        await asyncio.sleep(seconds)
