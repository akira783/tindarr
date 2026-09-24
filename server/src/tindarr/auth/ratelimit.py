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
bounded by ``max_keys``. What happens when that table is full is a policy, because the
two possible answers are both dangerous somewhere:

- ``when_full="evict_oldest"`` drops the least recently touched key. Nobody is ever
  refused for somebody else's traffic, which is what a limit keyed by client address
  wants — but a flood of fresh keys can push a live bucket out and give its owner a
  clean slate.
- ``when_full="refuse"`` never drops a bucket that still holds events inside its
  window: it prunes the expired ones and, if every remaining bucket is live, refuses
  the new key with ``429``. That is what a cap whose whole job is to protect something
  outside Tindarr needs (``password_per_username`` guards the media server's own
  lockout counter), and it is why that limiter has its own instance.

Windows are measured on the monotonic clock, so a system clock jump cannot open a hole.
"""

from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal

from tindarr.auth.events import security_event
from tindarr.core.clock import Clock
from tindarr.core.errors import RateLimitedError

#: How many distinct keys one limiter remembers.
DEFAULT_MAX_KEYS: Final = 10_000
#: How many requests one ``ExponentialPause`` may hold asleep at the same time.
DEFAULT_MAX_WAITING: Final = 50
_MS: Final = 1000

#: What a limiter does when its key table is full (see the module docstring).
type WhenFull = Literal["evict_oldest", "refuse"]


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


def _refuse(limit_name: str, retry_after_ms: int, reason: str = "over_limit") -> RateLimitedError:
    # The key is never logged: it may be a user name. The access log already has the
    # request id and the client address.
    security_event("rate_limited", limit=limit_name, retry_after_ms=retry_after_ms, reason=reason)
    return RateLimitedError(retry_after_ms)


class _TableFullError(Exception):
    """Every remembered bucket is live: a ``refuse`` limiter admits no new key."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__("the limiter's key table is full of live buckets")
        self.retry_after_s = retry_after_s


class _Events:
    """Timestamps per key, pruned on access, bounded by ``max_keys``.

    ``when_full`` decides what an eleventh-thousandth key costs (module docstring):
    the least recently touched bucket, or the new key itself.
    """

    def __init__(self, window: float, max_keys: int, when_full: WhenFull) -> None:
        self._window = window
        self._max_keys = max_keys
        self._when_full = when_full
        self._keys: OrderedDict[str, deque[float]] = OrderedDict()
        # Set by a scan that found no room: before then, no bucket can lose an event,
        # so there is nothing to scan for and the answer cannot change.
        self._full_until: float | None = None

    def times(self, key: str, now: float) -> deque[float]:
        """Return this key's events inside the window, oldest first.

        Raises ``_TableFullError`` when the key is new, the table is full and this limiter
        refuses rather than evicting.
        """
        times: deque[float] | None = self._keys.get(key)
        if times is not None:
            self._keys.move_to_end(key)
            self._prune(times, now)
            return times
        if len(self._keys) >= self._max_keys:
            self._make_room(now)
        times = deque[float]()
        self._keys[key] = times
        return times

    def _prune(self, times: deque[float], now: float) -> None:
        while times and times[0] <= now - self._window:
            times.popleft()

    def _make_room(self, now: float) -> None:
        """Drop what expired; evict or raise when every remaining bucket is live."""
        if self._when_full == "evict_oldest":
            while len(self._keys) >= self._max_keys:
                self._keys.popitem(last=False)
            return
        if self._full_until is not None and now < self._full_until:
            raise _TableFullError(self._full_until - now)
        oldest_event = now
        for stored, times in list(self._keys.items()):
            self._prune(times, now)
            if not times:
                del self._keys[stored]
                continue
            oldest_event = min(oldest_event, times[0])
        if len(self._keys) < self._max_keys:
            self._full_until = None
            return
        # The first event of the oldest bucket is the first moment anything can change.
        retry_after = max(oldest_event + self._window - now, 0.0)
        self._full_until = now + retry_after
        raise _TableFullError(retry_after)

    def record(self, key: str, now: float) -> int:
        """Add one event for this key and return how many are now in the window.

        A table so full that the key cannot even be created drops the event: every
        ``check`` for an unknown key is refused for as long as that lasts, so the
        guarantee the caller bought still holds (the refusal is just not remembered).
        """
        try:
            times = self.times(key, now)
        except _TableFullError:
            return 0
        times.append(now)
        return len(times)

    def forget(self, key: str) -> None:
        """Drop everything remembered about this key (a success clears its failures)."""
        if self._keys.pop(key, None) is not None:
            self._full_until = None  # a slot just came free

    @property
    def tracked(self) -> int:
        """How many keys the table holds right now (its memory bound, for the tests)."""
        return len(self._keys)


class SlidingWindow:
    """Refuses more than ``limit.count`` events per key in a rolling window."""

    def __init__(
        self,
        limit: Limit,
        clock: Clock,
        max_keys: int = DEFAULT_MAX_KEYS,
        when_full: WhenFull = "evict_oldest",
    ) -> None:
        self._limit = limit
        self._clock = clock
        self._events = _Events(limit.seconds, max_keys, when_full)

    @property
    def limit(self) -> Limit:
        """The limit this window enforces."""
        return self._limit

    def check(self, key: str) -> None:
        """Raise ``429 rate_limited`` when the key already reached the limit.

        A ``refuse`` limiter whose table is full of live buckets also answers ``429``:
        it cannot tell whether this key is under its cap, and the whole point of that
        policy is that the doubt costs the caller, never the bucket somebody else owns.
        """
        now = self._clock.monotonic()
        try:
            times = self._events.times(key, now)
        except _TableFullError as full:
            raise _refuse(
                self._limit.name, max(int(full.retry_after_s * _MS), 1), "key_table_full"
            ) from None
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
        try:
            return len(self._events.times(key, self._clock.monotonic()))
        except _TableFullError:
            return 0

    @property
    def tracked_keys(self) -> int:
        """How many keys this limiter remembers (never more than ``max_keys``)."""
        return self._events.tracked


class ExponentialPause:
    """Slows a key down past a threshold instead of refusing it.

    Used for failed password sign-ins: the pause doubles with each further failure, up
    to ``max_pause``, and disappears on its own when the window empties.

    A pause is a request held open, so — like ``GlobalSlowdown`` — only ``max_waiting``
    of them sleep at the same time; beyond that the request is refused right away
    instead of costing a socket and a task for up to fifteen minutes. Refusing there
    locks nobody out: the caller is already past the threshold that earns a pause, and
    a `429` is strictly friendlier than the wait it replaces.
    """

    def __init__(  # noqa: PLR0913, PLR0917 - the pause curve, the memory bound, the queue
        self,
        limit: Limit,
        clock: Clock,
        first_pause: timedelta = timedelta(seconds=1),
        max_pause: timedelta = timedelta(minutes=15),
        max_keys: int = DEFAULT_MAX_KEYS,
        max_waiting: int = DEFAULT_MAX_WAITING,
    ) -> None:
        self._limit = limit
        self._clock = clock
        self._first = first_pause.total_seconds()
        self._max = max_pause.total_seconds()
        self._events = _Events(limit.seconds, max_keys, "evict_oldest")
        self._max_waiting = max_waiting
        self._waiting = 0

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
        """Wait out the key's pause, if any; refuse once too many are already waiting."""
        pause = self.pause_for(key)
        if pause <= 0:
            return
        if self._waiting >= self._max_waiting:
            raise _refuse(self._limit.name, max(int(pause * _MS), 1), "too_many_waiting")
        self._waiting += 1
        try:
            security_event(
                "rate_limit_pause",
                limit=self._limit.name,
                seconds=int(pause),
                waiting=self._waiting,
            )
            await self._clock.sleep(pause)
        finally:
            self._waiting -= 1

    @property
    def waiting(self) -> int:
        """How many requests are being held asleep right now."""
        return self._waiting

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
        self._events = _Events(limit.seconds, 1, "evict_oldest")
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
        # The only limiter that refuses rather than evict a live bucket: it is what
        # stands between a guesser and the media server's own lockout counter, so a
        # flood of unknown user names must not be able to push a real one out of the
        # table (docs/auth.md, section 8).
        self.password_per_username = SlidingWindow(
            Limit("password_username", 2, quarter), clock, when_full="refuse"
        )
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
        # An upload is a megabyte of somebody's history and a few hundred TMDb requests.
        # Nobody imports their Netflix export five times an hour by accident, and the
        # per-user "one at a time" rule below it only bounds what runs, not what arrives.
        self.imports = SlidingWindow(Limit("import_upload", 5, timedelta(hours=1)), clock)
        # A calibration wall is answered a handful of times in an account's life, and
        # each answer is a row somebody's own database keeps for ever. The endpoint
        # takes any TMDb id, not only the ones a wall offered, so the only thing
        # standing between a household member and a database full of history rows is
        # this: twenty walls an hour, two hundred answers each.
        self.calibration = SlidingWindow(Limit("calibration_grid", 20, timedelta(hours=1)), clock)
