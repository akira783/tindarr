"""Answering "what do I show next?", and deciding when that costs a generation.

The deck endpoint has one job and four possible answers, and the order they are tried in
is the whole design:

1. **cards this user has already been served and not answered** — always first, so a
   client that lost its state loses nothing and a reload never spends money;
2. **a batch that was generated and never served** — the warm-up's, or the one the
   previous poll started. Serving it is what stamps the cards and feeds the next
   prompt's "already shown" list;
3. **a generation somebody is waiting for** — ``202`` and a delay, never a second job;
4. **a generation that failed** — ``502`` with its problem code, exactly once, then the
   deck goes back to trying.

Only after all four does it start one, and only then does it look at the cap.

**Two things are computed here rather than stored** (``tindarr.swipe.generation`` says
why the rest is not): whether each streaming offer is one the user pays for, and how far
the title has got in the request backend. Both depend on something that changes after
the card was built — a preference ticked, a request filed — so a card somebody is
already holding shows today's answer, which is what the contract promises.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr.core.clock import Clock
from tindarr.core.errors import PendingError, ProblemError
from tindarr.ports import problems
from tindarr.ports.deck import MediaFilter, Novelty
from tindarr.ports.metadata import INCLUDED_OFFERS, Provider
from tindarr.ports.request_backend import Availability
from tindarr.ports.titles import TitleRef
from tindarr.storage import batches as batch_repository
from tindarr.storage import history as history_repository
from tindarr.storage import jobs as job_repository
from tindarr.storage import profiles as profile_repository
from tindarr.storage import usage as usage_repository
from tindarr.storage import votes as vote_repository
from tindarr.storage.batches import StoredCard
from tindarr.storage.db import write_transaction
from tindarr.storage.profiles import DeckPreferences
from tindarr.storage.usage import DailyLimitReachedError, ProviderOption
from tindarr.storage.users import User
from tindarr.swipe.engine import (
    DECK_LOW_WATER,
    DECK_SIZE,
    DeckRequest,
    SwipeEngine,
    llm_not_configured,
    tmdb_not_configured,
)
from tindarr.swipe.generation import BatchGenerator, DeckMode
from tindarr.swipe.hybrid import PROMPT_VOTES, SEEN_RATIO_MIN_VOTES, SEEN_RATIO_WARNING
from tindarr.swipe.strategy import CALIBRATION_TARGET
from tindarr.swipe.votes import SEEN_VOTES

__all__ = [
    "GENERATION_RETRY_MS",
    "BatchScheduler",
    "Calibration",
    "DeckService",
    "DeckView",
    "ServedCard",
    "SwipeStatus",
    "daily_limit_reached",
]

logger = logging.getLogger(__name__)

#: How long a client waits before polling a generation again. A batch is a TMDb pool and
#: one model call; under two seconds the poll is noise, over five it feels broken.
GENERATION_RETRY_MS: Final = 2500
#: How long to wait when the server is busy with other people's generations. Longer,
#: because what the client is waiting for is not its own work.
BUSY_RETRY_MS: Final = 5000
#: How long the deck waits after a failed generation before paying for another.
#:
#: Without it a provider that is refusing a key spends the whole day's cap in half a
#: minute: a deck is polled every 2.5 s, nothing about the failure is remembered between
#: polls, and each poll is a fresh generation. The cap was meant to be a day's budget,
#: not thirty seconds of one. A household that fixes the key waits at most this long.
FAILURE_COOL_OFF: Final = timedelta(minutes=5)
#: How long an empty pool is believed before the deck tries again.
#:
#: A batch that came back with no cards means the pool had nothing, and asking again
#: immediately would produce the same nothing at the same price. But "nothing changed"
#: cannot be judged on votes alone — there are no cards to vote on — so without a clock
#: an account whose *first* batch was empty would have a blank deck for ever, with no
#: error and nothing an administrator would think to look at.
EXHAUSTED_COOL_OFF: Final = timedelta(hours=1)


def daily_limit_reached(limit: int) -> ProblemError:
    """429: this user has spent their generations for the day (the contract's code)."""
    return ProblemError(
        HTTPStatus.TOO_MANY_REQUESTS,
        "daily_limit_reached",
        f"You have used your {limit} AI batches for today; the deck comes back tomorrow.",
        headers={"Retry-After": "3600"},
        extensions={"retry_after_ms": 3_600_000},
    )


class BatchScheduler(Protocol):
    """Where a claimed generation actually runs.

    A protocol because the task lives in ``tindarr.jobs``, which sits **above** this
    layer: the deck decides that a batch is needed and who may pay for it, and something
    higher up owns the asyncio task that does it.
    """

    def has_capacity(self) -> bool:
        """Whether the server could run another generation right now."""
        ...

    def submit(self, user: User, request: DeckRequest, job_id: str) -> None:
        """Run one claimed generation in the background."""
        ...


@dataclass(frozen=True, slots=True)
class ServedCard:
    """One stored card, with the two things that depend on the moment it is shown."""

    card: StoredCard
    #: Provider ids of the user's own services, among this card's offers.
    subscribed: frozenset[int] = frozenset()
    availability: Availability = "none"


@dataclass(frozen=True, slots=True)
class Calibration:
    """How far the deck has got with finding out what this person likes."""

    done: int
    target: int = CALIBRATION_TARGET

    @property
    def complete(self) -> bool:
        """Whether the deck has stopped calibrating."""
        return self.done >= self.target


@dataclass(frozen=True, slots=True)
class DeckView:
    """What ``GET /swipe/deck`` answers when it has something to show."""

    mode: DeckMode
    novelty: Novelty
    cards: tuple[ServedCard, ...]
    calibration: Calibration
    seen_ratio_warning: bool = False
    #: The deck is empty because the pool had nothing to offer, not because a batch is
    #: on its way. Without it a client cannot tell "no cards yet" from "no cards at all",
    #: and both look like a bug.
    exhausted: bool = False


@dataclass(frozen=True, slots=True)
class SwipeStatus:
    """What the client needs to know before it shows a card."""

    llm_configured: bool = False
    llm_provider: str | None = None
    tmdb_configured: bool = False
    requests_enabled: bool = False
    media_history: bool = False
    streaming_region: str | None = None
    ratings_enabled: bool = False
    votes: int = 0
    calibration: Calibration = field(default_factory=lambda: Calibration(done=0))
    profile_ready: bool = False
    generations_left_today: int | None = None


class DeckService:
    """The read side of the deck: what to show, what to say, and when to pay."""

    def __init__(
        self,
        engine: AsyncEngine,
        swipe: SwipeEngine,
        generator: BatchGenerator,
        scheduler: BatchScheduler,
        clock: Clock,
    ) -> None:
        self._engine = engine
        self._swipe = swipe
        self._generator = generator
        self._scheduler = scheduler
        self._clock = clock

    async def preferences(self, user_id: str) -> DeckPreferences:
        """Return this user's stored preferences, or the defaults."""
        async with self._engine.connect() as connection:
            return await profile_repository.read_preferences(connection, user_id)

    async def update_preferences(
        self, user_id: str, patch: profile_repository.PreferencesPatch
    ) -> DeckPreferences:
        """Apply a partial change and return the whole of what the deck will now read."""
        async with write_transaction(self._engine) as connection:
            return await profile_repository.update_preferences(
                connection, user_id, patch, now=self._clock.now()
            )

    async def resolve(
        self,
        user_id: str,
        *,
        media_filter: MediaFilter | None,
        novelty: Novelty | None,
        mood: str | None,
        force_calibration: bool | None,
    ) -> DeckRequest:
        """Fold the query parameters into the stored preferences, query winning.

        A query parameter is a choice for this deck and not a change of mind: it is
        deliberately **not** written back, so switching to ``bold`` for one session does
        not quietly become somebody's setting.
        """
        stored = await self.preferences(user_id)
        return DeckRequest(
            media_filter=media_filter or stored.media_filter,
            novelty=novelty or stored.novelty,
            mood=mood,
            force_calibration=force_calibration,
        )

    async def deck(self, user: User, request: DeckRequest) -> DeckView:
        """Return the next cards, or raise ``PendingError`` while a batch is built."""
        now = self._clock.now()
        async with self._engine.connect() as connection:
            pending = await batch_repository.list_pending(
                connection, user.id, request.media_filter, now=now, limit=DECK_SIZE
            )
        if pending:
            await self._top_up(user, request, len(pending))
            return await self._view(user, request, pending)
        return await self._next_batch(user, request, now)

    async def _cooling_off(self, user: User, now: datetime) -> int:
        """Milliseconds left of the pause after a failed generation, or zero.

        Read before anything is claimed, so a provider that is down costs one generation
        every five minutes rather than one every poll.
        """
        async with self._engine.connect() as connection:
            last = await job_repository.last_finished(connection, user.id, "batch")
        if last is None or last.status != "failed" or last.finished_at is None:
            return 0
        left = (last.finished_at + FAILURE_COOL_OFF) - now
        return max(int(left.total_seconds() * 1000), 0)

    async def _next_batch(self, user: User, request: DeckRequest, now: datetime) -> DeckView:
        async with write_transaction(self._engine) as connection:
            ready = await batch_repository.ready_batch(connection, user.id, request.media_filter)
            served = (
                await batch_repository.serve_batch(connection, ready.id, user.id, now=now)
                if ready is not None
                else []
            )
        if served:
            logger.info("a batch was served", extra={"cards": len(served)})
            return await self._view(user, request, served)
        await self._report_failure(user)
        if await self._exhausted(user, request, now):
            # A finished batch that came back empty. Starting another one would produce
            # the same empty pool and bill for it, so the deck says so and stops — until
            # the cool-off, because a pool is not empty for ever.
            return await self._view(user, request, [], exhausted=True)
        await self._start(user, request)
        raise PendingError(GENERATION_RETRY_MS)

    async def _top_up(self, user: User, request: DeckRequest, pending: int) -> None:
        """Start the next batch while the deck still has cards, and say nothing about it.

        A generation this quiet must never be able to fail a request somebody is being
        answered: everything it could raise is swallowed, and the caller gets the cards
        it already had.
        """
        if pending >= DECK_LOW_WATER:
            return
        async with self._engine.connect() as connection:
            if await batch_repository.ready_batch(connection, user.id, request.media_filter):
                return
        try:
            await self._start(user, request)
        except Exception:  # noqa: BLE001 - nothing here may fail a request being answered
            # The caller is being handed cards it already had. Whatever stopped the
            # *next* batch — a cap, a cool-off, a busy server, a scheduler that could
            # not take the job — is not news for this request.
            logger.info("the deck could not prepare the next batch yet")

    async def _start(self, user: User, request: DeckRequest) -> None:
        """Claim a generation for this user and hand it to the scheduler."""
        await self._swipe.metadata()
        await self._swipe.llm()
        cooling = await self._cooling_off(user, self._clock.now())
        if cooling:
            # The last one failed a moment ago. Paying for another now is how a bad key
            # spends a day's budget in half a minute.
            raise PendingError(cooling)
        if not self._scheduler.has_capacity():
            raise PendingError(BUSY_RETRY_MS)
        try:
            job_id = await self._generator.claim(user, request)
        except DailyLimitReachedError as full:
            raise daily_limit_reached(full.limit) from None
        if job_id is None:
            # Somebody else's poll got there first; theirs is the one being waited for.
            return
        try:
            self._scheduler.submit(user, request, job_id)
        except Exception:
            # A claimed job nobody runs would hold this user's slot until a restart.
            await self._generator.fail(user, job_id, "internal_error")
            raise

    async def _report_failure(self, user: User) -> None:
        """Raise the last generation's failure, once, then never again for that job."""
        async with write_transaction(self._engine) as connection:
            if await job_repository.active(connection, user.id, "batch") is not None:
                raise PendingError(GENERATION_RETRY_MS)
            last = await job_repository.last_finished(connection, user.id, "batch")
            if last is None or last.status != "failed" or last.error_code is None:
                return
            told = await job_repository.mark_reported(connection, last.id, now=self._clock.now())
        problem = _failure_problem(last.error_code)
        if told and problem is not None:
            raise problem

    async def _exhausted(self, user: User, request: DeckRequest, now: datetime) -> bool:
        """Whether the last batch came back empty and nothing has changed since.

        Two things count as a change, and the second one is what stops a blank deck
        being permanent. A **vote** moves the seeds the pool is built from, so it is
        worth paying for another batch — but an account whose first batch was empty has
        no cards to vote on, and would otherwise be stuck for ever. So **time** counts
        too: TMDb gains titles, a content filter gets relaxed, a region changes. After
        the cool-off the deck tries once more.
        """
        async with self._engine.connect() as connection:
            last = await batch_repository.latest_batch(connection, user.id, request.media_filter)
            if last is None or last.cards_count > 0:
                return False
            if now - last.created_at >= EXHAUSTED_COOL_OFF:
                return False
            recent = await vote_repository.list_for_user(connection, user.id)
        return not any(vote.voted_at > last.created_at for vote in recent)

    # --- turning stored cards into what is shown --------------------------------------

    async def _view(
        self,
        user: User,
        request: DeckRequest,
        cards: Sequence[StoredCard],
        *,
        exhausted: bool = False,
    ) -> DeckView:
        preferences = await self.preferences(user.id)
        async with self._engine.connect() as connection:
            stored = await vote_repository.list_for_user(connection, user.id)
            counted = await vote_repository.count(connection, user.id)
        subscribed = frozenset(preferences.streaming_services)
        availability = await self._availability([card.ref for card in cards])
        return DeckView(
            mode="calibration" if counted < CALIBRATION_TARGET else "normal",
            novelty=request.novelty,
            cards=tuple(
                ServedCard(
                    card=card,
                    subscribed=_subscribed(card, subscribed),
                    availability=availability.get(card.ref, "none"),
                )
                for card in cards
            ),
            calibration=Calibration(done=min(counted, CALIBRATION_TARGET)),
            seen_ratio_warning=_seen_ratio_warning(stored),
            exhausted=exhausted,
        )

    async def _availability(self, refs: Sequence[TitleRef]) -> dict[TitleRef, Availability]:
        """Ask the request backend how far each title has got. Best effort.

        A backend that is down costs the "already requested" badge and nothing else: the
        card is still a card, and the request endpoint will report the real failure if
        somebody tries to act on it.
        """
        if not refs:
            return {}
        backend = await self._swipe.request_backend()
        if backend is None:
            return {}
        try:
            return await backend.status(refs)
        except ProblemError as failure:
            logger.info("the request backend did not answer", extra={"reason": failure.code})
            return {}

    # --- the two read-only screens ------------------------------------------------------

    async def status(self, user: User) -> SwipeStatus:
        """Everything the client needs before it shows its first card."""
        place = await self._swipe.household()
        tmdb, llm = await self._swipe.configured()
        provider = await self._swipe.llm_name()
        backend = await self._swipe.request_backend()
        ratings = await self._swipe.ratings()
        now = self._clock.now()
        async with self._engine.connect() as connection:
            counted = await vote_repository.count(connection, user.id)
            profile = await profile_repository.read_profile(connection, user.id)
            history = await history_repository.seen_refs(connection, user.id)
            spent = await usage_repository.count_today(connection, user.id, now=now)
        limit = (
            user.daily_generation_limit
            if user.daily_generation_limit is not None
            else place.daily_generation_limit
        )
        engagement = bool(history) or bool(await self._swipe.engagement(user))
        return SwipeStatus(
            llm_configured=llm,
            llm_provider=provider,
            tmdb_configured=tmdb,
            requests_enabled=backend is not None,
            media_history=engagement,
            streaming_region=place.region,
            ratings_enabled=ratings is not None,
            votes=counted,
            calibration=Calibration(done=min(counted, CALIBRATION_TARGET)),
            profile_ready=profile is not None and bool(profile.text),
            generations_left_today=max(limit - spent, 0),
        )

    async def region_providers(self) -> tuple[str, tuple[ProviderOption, ...]]:
        """Return the region and the streaming services available there, cached 7 days.

        Stale beats absent: a list a fortnight old is still the right set of logos, and
        a preferences screen that cannot be opened because TMDb is slow is worse than
        one that is a release behind. Only an empty cache turns a TMDb failure into an
        error.
        """
        place = await self._swipe.household()
        region = place.region
        now = self._clock.now()
        async with self._engine.connect() as connection:
            cached = await usage_repository.read_region_providers(connection, region)
        if cached is not None and cached.fresh(now):
            return region, cached.providers
        metadata = await self._swipe.metadata()
        try:
            found = await metadata.region_providers(region)
        except ProblemError:
            if cached is None:
                raise problems.metadata_unreachable() from None
            logger.info("serving a stale provider list", extra={"region": region})
            return region, cached.providers
        options = tuple(_as_option(provider) for provider in found)
        async with write_transaction(self._engine) as connection:
            await usage_repository.write_region_providers(connection, region, options, now=now)
        return region, options


def _as_option(provider: Provider) -> ProviderOption:
    return ProviderOption(
        provider_id=provider.provider_id, name=provider.name, logo_path=provider.logo_path
    )


def _subscribed(card: StoredCard, services: frozenset[int]) -> frozenset[int]:
    """Which of this card's offers are on services the user pays for.

    Only the included offers count (``subscription``, ``free``, ``ads``): being able to
    rent a film on a shop somebody has an account with is not "it is on your services",
    and the port says which three those are so the badge and the prompt agree.
    """
    return frozenset(
        offer.provider_id
        for offer in card.providers
        if offer.provider_id in services and offer.offer in INCLUDED_OFFERS
    )


def _seen_ratio_warning(stored: Sequence[vote_repository.VoteRecord]) -> bool:
    """Whether enough recent cards were already known to suggest a bolder setting.

    The same two numbers the prompt uses (``tindarr.swipe.hybrid``), so the nudge the
    client shows and the sentence the model is given cannot disagree about when somebody
    is being served things they have already watched.
    """
    recent = sorted(stored, key=lambda vote: vote.voted_at, reverse=True)
    opinions = [vote for vote in recent if vote.value != "skip"][:PROMPT_VOTES]
    if len(opinions) < SEEN_RATIO_MIN_VOTES:
        return False
    seen = sum(1 for vote in opinions if vote.value in SEEN_VOTES)
    return seen / len(opinions) >= SEEN_RATIO_WARNING


#: The failures the contract's ``502`` on the deck names, by their stored code.
_REPORTED_FAILURES: Final = {
    "llm_auth_failed": problems.llm_auth_failed,
    "llm_quota": problems.llm_quota,
    "llm_model_not_found": problems.llm_model_not_found,
    "llm_unreachable": problems.llm_unreachable,
    "llm_invalid_output": problems.llm_invalid_output,
    "metadata_unreachable": problems.metadata_unreachable,
    "tmdb_not_configured": tmdb_not_configured,
    "llm_not_configured": llm_not_configured,
}


#: Failures the deck latches without telling anybody. See ``_failure_problem``.
_SILENT_FAILURES: Final = frozenset({"interrupted", "internal_error"})


def _failure_problem(code: str) -> ProblemError | None:
    """Turn a stored job failure into the problem to raise, or ``None`` to stay quiet.

    Two are deliberately silent. ``interrupted`` is a restart: not something the user
    did, nothing that will be different next time, and the poll that found it is one
    generation away from a deck. ``internal_error`` is a bug on this side that has
    already been logged with its traceback — and, if a request was in flight when it
    happened, already answered with a ``500``; a second error screen on the next poll
    would report one event twice. Both are still latched as reported, so the next poll
    starts a batch rather than looking at them again.
    """
    build = _REPORTED_FAILURES.get(code)
    if build is not None:
        return build()
    if code in _SILENT_FAILURES:
        return None
    return ProblemError(
        HTTPStatus.BAD_GATEWAY,
        "llm_unreachable",
        "The last batch could not be generated; try again.",
    )
