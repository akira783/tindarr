"""In-memory rate limits (docs/auth.md, section 8).

Three primitives, all keyed by whatever the caller passes (a client address, a
case-folded user name, a session id):

- ``SlidingWindow`` refuses above N events in a rolling window (``429 rate_limited``
  with ``retry_after_ms``);
- ``ExponentialPause`` never refuses: past its threshold it delays the answer, doubling
  each further failure up to a cap, which slows a password guesser without locking
  anyone out;
- ``GlobalSlowdown`` is the server-wide guard: above its threshold every request waits
  an extra second, and only the requests beyond ``max_waiting`` get a ``429``. An
  attacker can make everyone slower while their traffic lasts, never lock them out.

Counters live in memory: they are lost on restart (which needs host access anyway) and
bounded by ``max_keys``, the oldest key being dropped first.

Windows are measured on the monotonic clock, so a system clock jump cannot open a hole.
"""

from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from tindarr.auth.events import security_event
from tindarr.core.clock import Clock
from tindarr.core.errors import RateLimitedError

#: How many distinct keys one limiter remembers before dropping the oldest.
DEFAULT_MAX_KEYS: Final = 10_000
_MS: Final = 1000


@dataclass(frozen=True, slots=True)
class Limit:
    """``count`` events per ``window``, under the name used in the logs."""

    name: str
    count: int
    window: timedelta

    @property
    def seconds(self) -> float:
        """The window, in seconds."""
        return self.window.total_seconds()


def _refuse(limit_name: str, retry_after_ms: int) -> RateLimitedError:
    # The key is never logged: it may be a user name. The access log already has the
    # request id and the client address.
    security_event("rate_limited", limit=limit_name, retry_after_ms=retry_after_ms)
    return RateLimitedError(retry_after_ms)


class _Events:
    """Timestamps per key, pruned on access, with the oldest key dropped when full."""

    def __init__(self, window: float, max_keys: int) -> None:
        self._window = window
        self._max_keys = max_keys
        self._keys: OrderedDict[str, deque[float]] = OrderedDict()

    def times(self, key: str, now: float) -> deque[float]:
        """Return this key's events inside the window, oldest first."""
        times: deque[float] | None = self._keys.get(key)
        if times is None:
            times = deque[float]()
            self._keys[key] = times
        self._keys.move_to_end(key)
        while times and times[0] <= now - self._window:
            times.popleft()
        while len(self._keys) > self._max_keys:
            self._keys.popitem(last=False)
        return times

    def record(self, key: str, now: float) -> int:
        """Add one event for this key and return how many are now in the window."""
        times = self.times(key, now)
        times.append(now)
        return len(times)

    def forget(self, key: str) -> None:
        """Drop everything remembered about this key (a success clears its failures)."""
        self._keys.pop(key, None)


class SlidingWindow:
    """Refuses more than ``limit.count`` events per key in a rolling window."""

    def __init__(self, limit: Limit, clock: Clock, max_keys: int = DEFAULT_MAX_KEYS) -> None:
        self._limit = limit
        self._clock = clock
        self._events = _Events(limit.seconds, max_keys)

    @property
    def limit(self) -> Limit:
        """The limit this window enforces."""
        return self._limit

    def check(self, key: str) -> None:
        """Raise ``429 rate_limited`` when the key already reached the limit."""
        now = self._clock.monotonic()
        times = self._events.times(key, now)
        if len(times) >= self._limit.count:
            wait = times[0] + self._limit.seconds - now if times else self._limit.seconds
            raise _refuse(self._limit.name, max(int(wait * _MS), 1))

    def record(self, key: str) -> None:
        """Count one event against the key."""
        self._events.record(key, self._clock.monotonic())

    def hit(self, key: str) -> None:
        """Check the limit, then count this request (for limits that count every call)."""
        self.check(key)
        self.record(key)

    def forget(self, key: str) -> None:
        """Forget a key's events (a successful sign-in clears its failures)."""
        self._events.forget(key)

    def count(self, key: str) -> int:
        """How many events the key has inside the window."""
        return len(self._events.times(key, self._clock.monotonic()))


class ExponentialPause:
    """Slows a key down past a threshold instead of refusing it.

    Used for failed password sign-ins: the pause doubles with each further failure, up
    to ``max_pause``, and disappears on its own when the window empties.
    """

    def __init__(
        self,
        limit: Limit,
        clock: Clock,
        first_pause: timedelta = timedelta(seconds=1),
        max_pause: timedelta = timedelta(minutes=15),
        max_keys: int = DEFAULT_MAX_KEYS,
    ) -> None:
        self._limit = limit
        self._clock = clock
        self._first = first_pause.total_seconds()
        self._max = max_pause.total_seconds()
        self._events = _Events(limit.seconds, max_keys)

    @property
    def limit(self) -> Limit:
        """The threshold past which attempts are paused."""
        return self._limit

    def pause_for(self, key: str) -> float:
        """Seconds this key must wait before its next attempt is processed."""
        failures = len(self._events.times(key, self._clock.monotonic()))
        if failures < self._limit.count:
            return 0.0
        return min(self._first * 2 ** (failures - self._limit.count), self._max)

    async def wait(self, key: str) -> None:
        """Wait out the key's pause, if any."""
        pause = self.pause_for(key)
        if pause > 0:
            security_event("rate_limit_pause", limit=self._limit.name, seconds=int(pause))
            await self._clock.sleep(pause)

    def record_failure(self, key: str) -> None:
        """Count one failure against the key."""
        self._events.record(key, self._clock.monotonic())

    def forget(self, key: str) -> None:
        """Forget a key's failures (a success clears them)."""
        self._events.forget(key)


class GlobalSlowdown:
    """Server-wide guard that delays instead of refusing (docs/auth.md, section 8)."""

    def __init__(
        self,
        limit: Limit,
        clock: Clock,
        delay: timedelta = timedelta(seconds=1),
        max_waiting: int = 50,
    ) -> None:
        self._limit = limit
        self._clock = clock
        self._delay = delay.total_seconds()
        self._max_waiting = max_waiting
        self._events = _Events(limit.seconds, max_keys=1)
        self._waiting = 0

    @property
    def limit(self) -> Limit:
        """The threshold above which requests are slowed down."""
        return self._limit

    async def admit(self) -> None:
        """Let the request through, after a delay once the global threshold is passed."""
        now = self._clock.monotonic()
        if len(self._events.times(self._limit.name, now)) < self._limit.count:
            return
        if self._waiting >= self._max_waiting:
            raise _refuse(self._limit.name, int(self._delay * _MS))
        self._waiting += 1
        try:
            security_event("rate_limit_slowdown", limit=self._limit.name, waiting=self._waiting)
            await self._clock.sleep(self._delay)
        finally:
            self._waiting -= 1

    def record(self) -> None:
        """Count one event towards the global threshold."""
        self._events.record(self._limit.name, self._clock.monotonic())

    @property
    def waiting(self) -> int:
        """How many requests are being slowed down right now."""
        return self._waiting


class RateLimits:
    """Every limit of docs/auth.md, section 8, built once and shared by the routes.

    Step 2a uses the claim, refresh and public limits; 2b plugs its password and handle
    limits in, 2c the pairing ones. They are declared here so the numbers live in one
    place and the tests can read them.
    """

    def __init__(self, clock: Clock) -> None:
        quarter = timedelta(minutes=15)
        minute = timedelta(minutes=1)
        self.claim_failures = SlidingWindow(Limit("setup_claim", 5, quarter), clock)
        self.claim_failures_global = GlobalSlowdown(
            Limit("setup_claim_global", 20, timedelta(hours=1)), clock
        )
        self.password_failures = ExponentialPause(Limit("password_failures", 5, quarter), clock)
        self.password_per_username = SlidingWindow(Limit("password_username", 2, quarter), clock)
        self.handle_creation = SlidingWindow(Limit("handle_creation", 10, quarter), clock)
        # Preview and pair are the two calls somebody could grind against a code, and
        # an app makes each of them once. Completion is polled every two seconds for up
        # to five minutes, so it gets a budget that fits that cadence for the two or
        # three phones a household pairs at a time; a wrong verifier still gets it
        # nowhere (docs/auth.md, sections 8 and 9).
        self.pairing = SlidingWindow(Limit("pairing", 10, minute), clock)
        self.pairing_complete = SlidingWindow(Limit("pairing_complete", 90, minute), clock)
        self.pairing_global = GlobalSlowdown(Limit("pairing_global", 300, minute), clock)
        self.refresh = SlidingWindow(Limit("token_refresh", 30, minute), clock)
        self.public = SlidingWindow(Limit("public", 60, minute), clock)
        self.connection_tests = SlidingWindow(Limit("connection_test", 10, minute), clock)
