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


class BlockingClock(FakeClock):
    """A clock whose ``sleep`` really suspends, to observe requests waiting together."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        """Wait until the test releases the sleepers."""
        self.slept.append(seconds)
        await self.release.wait()


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


def test_an_evicting_window_forgets_the_oldest_keys_when_full(clock: FakeClock) -> None:
    # The default policy for limits keyed by client address: a full table costs the
    # least recently seen key, never a `429` to a caller who did nothing.
    window = SlidingWindow(Limit("test", 1, MINUTE), clock, max_keys=2)
    window.record("first")
    window.record("second")
    window.record("third")
    window.check("first")  # dropped to keep the limiter's memory bounded
    with pytest.raises(RateLimitedError):
        window.check("third")


def test_a_refusing_window_never_drops_a_live_bucket(clock: FakeClock) -> None:
    """The per-username cap must survive a flood of unknown names (H1).

    Without this, ~`max_keys` sign-in attempts with random user names push the account
    under attack out of the table and hand the guesser a clean slate — and the media
    server's own lockout counter is what pays for it.
    """
    window = SlidingWindow(Limit("test", 2, MINUTE), clock, max_keys=4, when_full="refuse")
    window.record("admin")  # one failure already forwarded for the account under attack

    for index in range(1_000):
        window.check(f"flood-{index}")  # aborted before any failure is recorded

    assert window.tracked_keys <= 4  # memory is still bounded
    assert window.count("admin") == 1  # and the bucket that matters is still there
    window.record("admin")
    with pytest.raises(RateLimitedError):
        window.check("admin")


def test_a_refusing_window_refuses_the_new_key_when_every_bucket_is_live(
    clock: FakeClock,
) -> None:
    window = SlidingWindow(Limit("test", 2, MINUTE), clock, max_keys=2, when_full="refuse")
    window.record("a")
    window.record("b")
    with pytest.raises(RateLimitedError) as caught:
        window.check("c")
    assert caught.value.retry_after_ms > 0
    window.check("a")  # the live buckets are untouched
    assert (window.count("a"), window.count("b")) == (1, 1)
    clock.advance(61)  # they expired: the table has room again
    window.check("c")
    assert window.tracked_keys <= 2


def test_a_success_frees_a_slot_in_a_full_refusing_window(clock: FakeClock) -> None:
    window = SlidingWindow(Limit("test", 2, MINUTE), clock, max_keys=2, when_full="refuse")
    window.record("a")
    window.record("b")
    with pytest.raises(RateLimitedError):
        window.check("c")
    window.forget("a")  # a successful sign-in clears its failures
    window.check("c")


def test_a_full_refusing_window_answers_for_a_key_it_cannot_create(clock: FakeClock) -> None:
    # Nothing is remembered about an unknown key while the table is full — and nothing
    # needs to be, because every `check` for one is refused until a slot frees up.
    window = SlidingWindow(Limit("test", 2, MINUTE), clock, max_keys=1, when_full="refuse")
    window.record("a")
    assert window.count("b") == 0
    window.record("b")
    assert window.tracked_keys == 1
    with pytest.raises(RateLimitedError):
        window.check("b")


def test_a_refusing_window_stays_bounded_while_it_is_flooded(clock: FakeClock) -> None:
    window = SlidingWindow(Limit("test", 2, MINUTE), clock, max_keys=8, when_full="refuse")
    refused = 0
    for index in range(500):
        key = f"flood-{index}"
        try:
            window.check(key)
        except RateLimitedError:
            refused += 1
            continue
        window.record(key)
    assert refused > 0  # the flood is refused instead of evicting somebody
    assert window.tracked_keys <= 8


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


async def test_only_the_requests_beyond_the_queue_are_paused() -> None:
    """A pause holds a request, a socket and a task: bound how many at once (M3)."""
    clock = BlockingClock()
    pause = ExponentialPause(Limit("test", 1, MINUTE), clock, max_waiting=2)
    for key in ("a", "b", "c"):
        pause.record_failure(key)
    waiting = [asyncio.create_task(pause.wait(key)) for key in ("a", "b")]
    await asyncio.sleep(0)
    assert pause.waiting == 2
    with pytest.raises(RateLimitedError) as caught:
        await pause.wait("c")
    assert caught.value.retry_after_ms > 0
    clock.release.set()
    await asyncio.gather(*waiting)
    assert pause.waiting == 0


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
