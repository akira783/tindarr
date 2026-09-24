"""Recording answers, undoing them, and the two lists they produce.

Everything a client sends about a vote is a **card id and one of five words**. The
title, the year, the poster and the pick type come off this server's own row for that
card, checked against this user's id (ADR 0007). That is the difference between a
statistic and a number the client writes.

Two properties are worth naming because they look like edge cases and are not:

- **An expired card can still be voted on.** A phone that was in a tunnel comes back
  with a queue, and the cards in it left the deck hours ago. ``get_card`` deliberately
  ignores the expiry; only the purge, a month later, ends it.
- **A re-sent queue changes nothing twice.** Each item carries a ``client_vote_id``, and
  the receipt for it is written in the same transaction as the vote. The second arrival
  is ``duplicate`` — not "stored again", which would resurrect a vote the user has since
  undone, and not "rejected", which would make a client retry for ever.

The taste profile is nudged from here rather than from a schedule: a rewrite is worth
paying for when somebody has actually said something, and a debounce on the vote is the
cheapest place to know that.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr.core.clock import Clock
from tindarr.ports.deck import PickKind, VoteValue
from tindarr.ports.request_backend import Availability, RequestStatus
from tindarr.ports.titles import TitleRef
from tindarr.storage import batches as batch_repository
from tindarr.storage import profiles as profile_repository
from tindarr.storage import votes as vote_repository
from tindarr.storage.batches import StoredCard
from tindarr.storage.db import write_transaction
from tindarr.storage.users import User
from tindarr.storage.votes import LikeCursor, NewVote, VoteRecord
from tindarr.swipe.engine import SwipeEngine
from tindarr.swipe.requesting import RequestService
from tindarr.swipe.votes import OPINION_VOTES, POSITIVE_VOTES

__all__ = [
    "PROFILE_REFRESH_VOTES",
    "LikeEntry",
    "PickStats",
    "ProfileScheduler",
    "Stats",
    "SubmittedVote",
    "VoteOutcome",
    "VoteService",
]

logger = logging.getLogger(__name__)

#: How many new opinions are worth a profile rewrite. The fork's cadence: often enough
#: that the deck follows a taste as it moves, rare enough that a swiping session is one
#: rewrite and not twenty.
PROFILE_REFRESH_VOTES: Final = 10


class ProfileScheduler(Protocol):
    """Where a debounced profile rewrite runs. Implemented in ``tindarr.jobs``."""

    async def request_rewrite(self, user: User) -> bool:
        """Start one rewrite for this user, or return ``False`` if it cannot run now."""
        ...


@dataclass(frozen=True, slots=True)
class SubmittedVote:
    """One item of a vote submission, as it arrives."""

    client_vote_id: str
    card_id: str
    value: VoteValue
    voted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class VoteOutcome:
    """What became of one submitted item, in the contract's own words."""

    client_vote_id: str
    outcome: Literal["stored", "duplicate", "rejected"]
    code: str | None = None
    request_status: RequestStatus | None = None


@dataclass(frozen=True, slots=True)
class LikeEntry:
    """One liked title, with what has happened to it since."""

    vote: VoteRecord
    availability: Availability = "none"
    watch_url: str | None = None


@dataclass(frozen=True, slots=True)
class PickStats:
    """Cards and likes for one pick type."""

    total: int = 0
    likes: int = 0


@dataclass(frozen=True, slots=True)
class Stats:
    """What the user has answered, with ``skip`` counted apart from everything."""

    total: int = 0
    likes: int = 0
    dislikes: int = 0
    seen_liked: int = 0
    seen_disliked: int = 0
    skips: int = 0
    requested: int = 0
    by_pick_type: dict[PickKind, PickStats] = field(default_factory=dict[PickKind, PickStats])

    @property
    def like_rate(self) -> float:
        """Share of opinions that were positive. ``skip`` is in neither half."""
        return (self.likes + self.seen_liked) / self.total if self.total else 0.0

    @property
    def request_rate(self) -> float:
        """Share of opinions that became a request."""
        return self.requested / self.total if self.total else 0.0


class VoteService:
    """Storing answers, undoing them, and answering the two lists they feed."""

    def __init__(
        self,
        engine: AsyncEngine,
        swipe: SwipeEngine,
        requests: RequestService,
        profiles: ProfileScheduler,
        clock: Clock,
    ) -> None:
        self._engine = engine
        self._swipe = swipe
        self._requests = requests
        self._profiles = profiles
        self._clock = clock

    async def submit(
        self, user: User, items: Sequence[SubmittedVote]
    ) -> tuple[list[VoteOutcome], bool]:
        """Store a queue of answers and say what became of each, in the order they came.

        Items are independent: one unknown card does not cost the other nine, which is
        what an offline queue needs, because the card it cannot find is usually one this
        user reset away weeks ago.
        """
        now = self._clock.now()
        liked, outcomes = await self._store(user, items, now)
        requested = await self._auto_request(user, tuple(liked.values()))
        refreshed = await self._maybe_refresh(user)
        answered = [
            _with_request(outcome, requested.get(liked.get(outcome.client_vote_id, _NOWHERE)))
            for outcome in outcomes
        ]
        return answered, refreshed

    async def _store(
        self, user: User, items: Sequence[SubmittedVote], now: datetime
    ) -> tuple[dict[str, TitleRef], list[VoteOutcome]]:
        """Write every acceptable item in one transaction, receipts included."""
        liked: dict[str, TitleRef] = {}
        outcomes: list[VoteOutcome] = []
        async with write_transaction(self._engine) as connection:
            for item in items:
                card = await batch_repository.get_card(connection, user.id, item.card_id)
                if card is None:
                    # Unknown, purged, or somebody else's. One answer for all three: a
                    # caller must not be able to tell another user's card id from a
                    # made-up one.
                    outcomes.append(VoteOutcome(item.client_vote_id, "rejected", "card_not_found"))
                    continue
                fresh = await vote_repository.remember_receipt(
                    connection, user.id, item.client_vote_id, now=now
                )
                if not fresh:
                    outcomes.append(VoteOutcome(item.client_vote_id, "duplicate"))
                    continue
                await vote_repository.record(
                    connection,
                    user.id,
                    NewVote(
                        ref=card.ref,
                        value=item.value,
                        card_id=card.id,
                        pick_type=card.pick_type,
                        title=card.title,
                        year=card.year,
                        poster_path=card.poster_path,
                    ),
                    voted_at=_when(item.voted_at, card, now),
                    now=now,
                )
                outcomes.append(VoteOutcome(item.client_vote_id, "stored"))
                if item.value == "like":
                    liked[item.client_vote_id] = card.ref
        return liked, outcomes

    async def _auto_request(
        self, user: User, liked: Sequence[TitleRef]
    ) -> dict[TitleRef, RequestStatus]:
        """File the likes, if this user asked for that. Never fails a vote."""
        if not liked:
            return {}
        async with self._engine.connect() as connection:
            preferences = await profile_repository.read_preferences(connection, user.id)
        if not preferences.auto_request:
            return {}
        filed: dict[TitleRef, RequestStatus] = {}
        for ref in dict.fromkeys(liked):
            status = await self._requests.auto(user, ref)
            if status is not None:
                filed[ref] = status
        return filed

    async def _maybe_refresh(self, user: User) -> bool:
        """Start a profile rewrite when enough has been said since the last one."""
        async with self._engine.connect() as connection:
            total = await vote_repository.count(connection, user.id)
            profile = await profile_repository.read_profile(connection, user.id)
        since = total - (profile.votes_at_update if profile is not None else 0)
        if since < PROFILE_REFRESH_VOTES:
            return False
        return await self._profiles.request_rewrite(user)

    async def undo(self, user: User, ref: TitleRef) -> bool:
        """Remove this user's answer about one title; return whether there was one.

        A request already filed is deliberately not cancelled: it is a row in somebody
        else's queue, possibly already downloading, and undoing a swipe is not a claim
        about that.
        """
        async with write_transaction(self._engine) as connection:
            return await vote_repository.remove(connection, user.id, ref)

    async def reset(self, user: User) -> int:
        """Delete every vote of this user; return how many went."""
        async with write_transaction(self._engine) as connection:
            gone = await vote_repository.delete_all(connection, user.id)
        logger.info("a user reset their votes", extra={"votes": gone})
        return gone

    async def likes(
        self,
        user: User,
        *,
        status: Literal["all", "to_request", "requested"] = "all",
        limit: int = 50,
        before: LikeCursor | None = None,
    ) -> list[LikeEntry]:
        """Return the "to request" list, with availability and a watch link if we own it."""
        wanted = {"all": None, "to_request": False, "requested": True}[status]
        async with self._engine.connect() as connection:
            found = await vote_repository.list_likes(
                connection, user.id, requested=wanted, limit=limit, before=before
            )
        if not found:
            return []
        refs = [vote.ref for vote in found]
        availability = await self._availability(refs)
        links = await self._swipe.watch_links(refs)
        return [
            LikeEntry(
                vote=vote,
                availability=availability.get(vote.ref, "none"),
                watch_url=links.get(vote.ref),
            )
            for vote in found
        ]

    async def _availability(self, refs: Sequence[TitleRef]) -> dict[TitleRef, Availability]:
        backend = await self._swipe.request_backend()
        if backend is None:
            return {}
        try:
            return await backend.status(refs)
        except Exception:  # noqa: BLE001 - a list of likes is not worth a failed request
            logger.info("the request backend did not answer for a likes list")
            return {}

    async def stats(self, user: User) -> Stats:
        """Count what this user has answered, with ``skip`` outside every rate."""
        async with self._engine.connect() as connection:
            found = await vote_repository.list_for_user(connection, user.id)
        return _stats(found)


def _stats(found: Sequence[VoteRecord]) -> Stats:
    """Build the statistics, counting each vote once and ``skip`` nowhere near a rate."""
    opinions = [vote for vote in found if vote.value in OPINION_VOTES]
    by_pick: dict[PickKind, PickStats] = {}
    for vote in opinions:
        current = by_pick.get(vote.pick_type, PickStats())
        by_pick[vote.pick_type] = PickStats(
            total=current.total + 1,
            likes=current.likes + (1 if vote.value in POSITIVE_VOTES else 0),
        )
    return Stats(
        total=len(opinions),
        likes=sum(1 for vote in opinions if vote.value == "like"),
        dislikes=sum(1 for vote in opinions if vote.value == "dislike"),
        seen_liked=sum(1 for vote in opinions if vote.value == "seen_liked"),
        seen_disliked=sum(1 for vote in opinions if vote.value == "seen_disliked"),
        skips=sum(1 for vote in found if vote.value == "skip"),
        requested=sum(1 for vote in opinions if vote.requested),
        by_pick_type=by_pick,
    )


def _with_request(outcome: VoteOutcome, status: RequestStatus | None) -> VoteOutcome:
    """Add the request an automatic filing produced, when there was one."""
    if status is None:
        return outcome
    return VoteOutcome(outcome.client_vote_id, outcome.outcome, outcome.code, status)


def _when(given: datetime | None, card: StoredCard, now: datetime) -> datetime:
    """When the user voted, bounded at both ends by facts this server already has.

    An offline queue carries the moment the swipe happened, which is the honest
    timestamp and the one the 60-day skip cool-down runs from. But it is a value the
    client chooses, so it is clamped:

    - **not after now**, or a drifting clock parks a vote at the top of every ordering
      for as long as the drift lasts;
    - **not before the card was served**, because a vote cannot predate the card it is
      about — and backdating a ``skip`` is otherwise a way to ask for the cool-down to
      be over already, since a skip older than sixty days is a title the deck is free
      to offer again.
    """
    floor = card.served_at or card.created_at
    if given is None:
        return now
    return min(max(given, floor), now)


#: A title no card can be: the "not a like" key of the per-item request lookup.
_NOWHERE: Final = TitleRef("movie", 1)
