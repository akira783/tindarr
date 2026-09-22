"""Rate limits: refusals, pauses and the global slowdown (docs/auth.md, section 8)."""

import asyncio
from datetime import timedelta

import pytest

from tests.support import FakeClock
from tindarr.auth.ratelimit import (
    ExponentialPause,
    GlobalSlowdown,
    Limit,
    RateLimits,
    SlidingWindow,
)
from tindarr.core.errors import ProblemError, RateLimitedError

pytestmark = pytest.mark.anyio

MINUTE = timedelta(minutes=1)


def test_a_window_allows_its_count_then_refuses(clock: FakeClock) -> None:
    window = SlidingWindow(Limit("test", 3, MINUTE), clock)
    for _ in range(3):
        window.hit("10.0.0.1")
    with pytest.raises(RateLimitedError) as caught:
        window.hit("10.0.0.1")
    assert caught.value.status == 429
    assert caught.value.code == "rate_limited"
    assert caught.value.extensions["retry_after_ms"] == 60_000
    assert caught.value.headers["Retry-After"] == "60"


def test_every_refusal_carries_a_wait_of_at_least_one_second(clock: FakeClock) -> None:
    window = SlidingWindow(Limit("test", 1, timedelta(milliseconds=1)), clock)
    window.record("k")
    with pytest.raises(RateLimitedError) as caught:
        window.check("k")
    assert caught.value.retry_after_ms >= 1
    assert int(caught.value.headers["Retry-After"]) >= 1


def test_a_window_slides(clock: FakeClock) -> None:
    window = SlidingWindow(Limit("test", 2, MINUTE), clock)
    window.record("k")
    clock.advance(30)
    window.record("k")
    with pytest.raises(RateLimitedError) as caught:
        window.check("k")
    assert caught.value.retry_after_ms == 30_000
    clock.advance(31)  # the first event left the window
    window.check("k")
    assert window.count("k") == 1


def test_keys_do_not_share_their_budget(clock: FakeClock) -> None:
    window = SlidingWindow(Limit("test", 1, MINUTE), clock)
    window.record("10.0.0.1")
    window.check("10.0.0.2")
    with pytest.raises(RateLimitedError):
        window.check("10.0.0.1")


def test_a_success_clears_the_failures(clock: FakeClock) -> None:
    window = SlidingWindow(Limit("test", 1, MINUTE), clock)
    window.record("k")
    window.forget("k")
    window.check("k")


def test_a_window_forgets_the_oldest_keys_when_full(clock: FakeClock) -> None:
    window = SlidingWindow(Limit("test", 1, MINUTE), clock, max_keys=2)
    window.record("first")
    window.record("second")
    window.record("third")
    window.check("first")  # dropped to keep the limiter's memory bounded
    with pytest.raises(RateLimitedError):
        window.check("third")


def test_the_pause_starts_after_the_threshold_and_doubles(clock: FakeClock) -> None:
    pause = ExponentialPause(Limit("test", 3, timedelta(minutes=15)), clock)
    for _ in range(3):
        assert pause.pause_for("10.0.0.1") == 0
        pause.record_failure("10.0.0.1")
    waits: list[float] = []
    for _ in range(4):
        waits.append(pause.pause_for("10.0.0.1"))
        pause.record_failure("10.0.0.1")
    assert waits == [1, 2, 4, 8]


def test_the_pause_is_capped_and_is_waited_out(clock: FakeClock) -> None:
    pause = ExponentialPause(
        Limit("test", 1, timedelta(minutes=15)), clock, max_pause=timedelta(seconds=4)
    )
    for _ in range(8):
        pause.record_failure("k")
    assert pause.pause_for("k") == 4


async def test_waiting_sleeps_for_the_pause(clock: FakeClock) -> None:
    pause = ExponentialPause(Limit("test", 1, MINUTE), clock)
    await pause.wait("k")  # no failure yet: no pause
    pause.record_failure("k")
    await pause.wait("k")
    pause.record_failure("k")
    await pause.wait("k")
    assert clock.slept == [1, 2]


def test_a_success_clears_the_pause(clock: FakeClock) -> None:
    pause = ExponentialPause(Limit("test", 1, MINUTE), clock)
    pause.record_failure("k")
    pause.record_failure("k")
    assert pause.pause_for("k") > 0
    pause.forget("k")
    assert pause.pause_for("k") == 0


async def test_the_global_guard_slows_down_instead_of_refusing(clock: FakeClock) -> None:
    guard = GlobalSlowdown(Limit("test", 2, MINUTE), clock)
    for _ in range(2):
        await guard.admit()
        guard.record()
    await guard.admit()
    assert clock.slept == [1.0]


class BlockingClock(FakeClock):
    """A clock whose ``sleep`` really suspends, to observe requests waiting together."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        """Wait until the test releases the sleepers."""
        self.slept.append(seconds)
        await self.release.wait()


async def test_only_the_requests_beyond_the_queue_are_refused() -> None:
    clock = BlockingClock()
    guard = GlobalSlowdown(Limit("test", 1, MINUTE), clock, max_waiting=2)
    guard.record()
    waiting = [asyncio.create_task(guard.admit()) for _ in range(2)]
    await asyncio.sleep(0)
    assert guard.waiting == 2
    with pytest.raises(RateLimitedError) as caught:
        await guard.admit()
    assert caught.value.retry_after_ms == 1000
    clock.release.set()
    await asyncio.gather(*waiting)
    assert guard.waiting == 0


def test_the_limits_match_the_documented_table(clock: FakeClock) -> None:
    limits = RateLimits(clock)
    assert (limits.claim_failures.limit.count, limits.claim_failures.limit.window) == (
        5,
        timedelta(minutes=15),
    )
    assert (limits.refresh.limit.count, limits.refresh.limit.window) == (30, MINUTE)
    assert (limits.public.limit.count, limits.public.limit.window) == (60, MINUTE)
    assert (limits.connection_tests.limit.count, limits.connection_tests.limit.window) == (
        10,
        MINUTE,
    )
    assert (limits.pairing.limit.count, limits.pairing.limit.window) == (10, MINUTE)
    # Completion is polled every two seconds for up to five minutes, so its budget has
    # to hold that cadence for more than one phone (docs/auth.md, section 8).
    assert (limits.pairing_complete.limit.count, limits.pairing_complete.limit.window) == (
        90,
        MINUTE,
    )
    assert limits.pairing_complete.limit.count > 2 * 60 // 2
    assert (limits.handle_creation.limit.count, limits.handle_creation.limit.window) == (
        10,
        timedelta(minutes=15),
    )
    # The per-username cap stays below Jellyfin's default lockout of three failures.
    assert limits.password_per_username.limit.count == 2
    assert (limits.password_failures.limit.count, limits.password_failures.limit.window) == (
        5,
        timedelta(minutes=15),
    )
    assert limits.claim_failures_global.limit.count == 20
    assert limits.pairing_global.limit.count == 300


def test_a_rate_limited_problem_is_a_problem_error(clock: FakeClock) -> None:
    window = SlidingWindow(Limit("test", 0, MINUTE), clock)
    with pytest.raises(ProblemError) as caught:
        window.check("k")
    assert caught.value.code == "rate_limited"
