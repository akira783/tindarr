"""The ``votes`` and ``vote_receipts`` tables: what somebody said, and what we already heard.

A vote is keyed by the **title**, not by the card. Two cards for the same film are one
opinion, voting again replaces the previous answer, and an undo is about a title
(ADR 0007). The card the answer came from is kept beside it, but only as provenance: it
goes null when the card is purged and the row stands, because the title, the year, the
poster and the pick type were copied off the card when the vote was stored.

That copy is the security property this module exists for. The client sends a card id
and a verdict; everything else on the row comes from a card that **this server** built
for **this user**. Nothing a phone sends can decide that a blockbuster was an
``explore`` pick, which is what would otherwise make the pick-type statistics a number
the client writes.

``vote_receipts`` answers a different question and outlives the vote: "have I already
stored this queued item?". An offline queue re-sends, a user undoes, a user changes
their mind — the vote row moves under all three, and only a separate receipt can say
"yes, and do not apply it again" after the vote it produced has gone.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import Row, Select, delete, func, literal, select, tuple_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from tindarr.ports.deck import PickKind, VoteValue, as_pick_kind, as_vote_value
from tindarr.ports.request_backend import RequestStatus
from tindarr.ports.titles import TitleRef, as_media_kind
from tindarr.storage.db import UtcDateTime
from tindarr.storage.tables import vote_receipts, votes

__all__ = [
    "IMPORT_RECEIPT_PREFIX",
    "RECEIPT_RETENTION",
    "SKIP_COOL_DOWN",
    "LikeCursor",
    "NewVote",
    "VoteRecord",
    "count",
    "counts_by_user",
    "delete_all",
    "get",
    "list_for_user",
    "list_likes",
    "mark_requested",
    "purge_receipts",
    "record",
    "remember_receipt",
    "remove",
    "seen_receipts",
    "skipped_refs",
]

#: How long a skipped title stays out of the deck (docs/architecture.md). "Not now" is
#: not "never": after this it is a candidate again.
SKIP_COOL_DOWN: Final = 60
#: How long a ``client_vote_id`` is remembered, in days. Longer than any queue a phone
#: could plausibly hold, and short enough that the table does not grow for ever.
RECEIPT_RETENTION: Final = 90
#: Receipts whose id starts with this are an **import's** bookkeeping and are kept for
#: ever (``purge_receipts``).
#:
#: The retention above is sized for a phone's offline queue, which is the only thing
#: that was ever meant to be in this table: after ninety days no client can still be
#: holding those items, so forgetting them is safe. An import's receipts answer a
#: question with no expiry date — "have these rows already been brought in?" — and
#: ``tindarr import suggestarr`` is exactly the command somebody runs again a year
#: later, having forgotten. Letting its receipts age out would turn a refusal into a
#: silent re-import of votes the person may have undone in the meantime.
IMPORT_RECEIPT_PREFIX: Final = "import:"


@dataclass(frozen=True, slots=True)
class LikeCursor:
    """Where a page of likes left off: the whole ordering key, not half of it."""

    at: datetime
    ref: TitleRef


@dataclass(frozen=True, slots=True)
class NewVote:
    """One answer, with the title data copied from the card the server served."""

    ref: TitleRef
    value: VoteValue
    card_id: str | None
    pick_type: PickKind
    title: str
    year: int | None = None
    poster_path: str | None = None


@dataclass(frozen=True, slots=True)
class VoteRecord:
    """One row of ``votes``."""

    ref: TitleRef
    value: VoteValue
    card_id: str | None
    pick_type: PickKind
    title: str
    year: int | None
    poster_path: str | None
    voted_at: datetime
    created_at: datetime
    requested_at: datetime | None
    request_status: str | None

    @property
    def requested(self) -> bool:
        """Whether this title was requested through Tindarr."""
        return self.requested_at is not None


async def record(
    connection: AsyncConnection, user_id: str, vote: NewVote, *, voted_at: datetime, now: datetime
) -> bool:
    """Store one answer, replacing this user's previous one about the same title.

    Returns whether it won. **The newest swipe wins, not the newest packet**: the update
    is guarded on ``voted_at``, so a queue a phone held in a drawer for three weeks
    cannot overwrite an opinion the user has since changed from their browser. Without
    the guard, arrival order decides — and an offline queue is precisely the client
    whose arrival order means nothing.

    ``requested_at`` is deliberately **not** reset by a later vote either: a request
    really filed with the backend stays filed, and telling the user otherwise would be a
    lie about somebody else's queue.
    """
    statement = sqlite_insert(votes).values(
        user_id=user_id,
        kind=vote.ref.kind,
        tmdb_id=vote.ref.tmdb_id,
        value=vote.value,
        card_id=vote.card_id,
        pick_type=vote.pick_type,
        title=vote.title,
        year=vote.year,
        poster_path=vote.poster_path,
        voted_at=voted_at,
        created_at=now,
    )
    result = await connection.execute(
        statement.on_conflict_do_update(
            index_elements=["user_id", "kind", "tmdb_id"],
            set_={
                name: statement.excluded[name]
                for name in ("value", "card_id", "pick_type", "title", "year", "poster_path")
            }
            | {"voted_at": statement.excluded["voted_at"]},
            where=votes.c.voted_at <= statement.excluded["voted_at"],
        )
    )
    return result.rowcount > 0


async def remember_receipt(
    connection: AsyncConnection, user_id: str, client_vote_id: str, *, now: datetime
) -> bool:
    """Write the receipt for a queued item; return ``False`` if it was already there.

    The insert is the test. Reading first and writing second would be two statements
    with a gap between them, and an offline queue that reconnects twice is exactly the
    traffic that finds such a gap.
    """
    result = await connection.execute(
        sqlite_insert(vote_receipts)
        .values(user_id=user_id, client_vote_id=client_vote_id, created_at=now)
        .on_conflict_do_nothing(index_elements=["user_id", "client_vote_id"])
    )
    return result.rowcount > 0


async def seen_receipts(
    connection: AsyncConnection, user_id: str, client_vote_ids: Sequence[str]
) -> frozenset[str]:
    """Return which of these queued items this user has already had stored."""
    if not client_vote_ids:
        return frozenset()
    statement = (
        select(vote_receipts.c.client_vote_id)
        .where(vote_receipts.c.user_id == user_id)
        .where(vote_receipts.c.client_vote_id.in_(list(client_vote_ids)))
    )
    return frozenset(str(row.client_vote_id) for row in (await connection.execute(statement)).all())


async def purge_receipts(connection: AsyncConnection, *, now: datetime) -> int:
    """Forget the receipts of queues no phone can still be holding.

    An import's receipts are exempt, whatever their age: they are not a queue and there
    is no moment after which re-applying them becomes harmless. See
    ``IMPORT_RECEIPT_PREFIX``.
    """
    result = await connection.execute(
        delete(vote_receipts)
        .where(vote_receipts.c.created_at < now - timedelta(days=RECEIPT_RETENTION))
        .where(~vote_receipts.c.client_vote_id.startswith(IMPORT_RECEIPT_PREFIX))
    )
    return result.rowcount


async def get(connection: AsyncConnection, user_id: str, ref: TitleRef) -> VoteRecord | None:
    """Return this user's current answer about one title, if they gave one."""
    row = (await connection.execute(_one(user_id, ref))).first()
    return None if row is None else _to_vote(row)


async def remove(connection: AsyncConnection, user_id: str, ref: TitleRef) -> bool:
    """Undo this user's answer about one title; return whether there was one."""
    result = await connection.execute(
        delete(votes)
        .where(votes.c.user_id == user_id)
        .where(votes.c.kind == ref.kind)
        .where(votes.c.tmdb_id == ref.tmdb_id)
    )
    return result.rowcount > 0


async def delete_all(connection: AsyncConnection, user_id: str) -> int:
    """Delete every vote of one user; return how many went.

    The taste profile is deliberately left standing: it is text about a person, some of
    it possibly written by them, and "start the deck again" is not "forget who I am".
    The receipts stay too, so a queue still in the air cannot re-create what was reset.
    """
    result = await connection.execute(delete(votes).where(votes.c.user_id == user_id))
    return result.rowcount


async def list_for_user(connection: AsyncConnection, user_id: str) -> list[VoteRecord]:
    """Return every vote of one user, oldest first."""
    statement = (
        votes.select()
        .where(votes.c.user_id == user_id)
        .order_by(votes.c.voted_at, votes.c.kind, votes.c.tmdb_id)
    )
    return _to_votes(await connection.execute(statement))


async def count(connection: AsyncConnection, user_id: str, *, skips: bool = False) -> int:
    """How many votes this user has cast. ``skip`` is left out unless asked for."""
    statement = select(func.count()).select_from(votes).where(votes.c.user_id == user_id)
    if not skips:
        statement = statement.where(votes.c.value != "skip")
    return int((await connection.execute(statement)).scalar_one())


async def counts_by_user(connection: AsyncConnection) -> dict[str, int]:
    """How many opinion votes each user has cast, for the console's user list."""
    statement = (
        select(votes.c.user_id, func.count().label("total"))
        .where(votes.c.value != "skip")
        .group_by(votes.c.user_id)
    )
    return {str(row.user_id): int(row.total) for row in (await connection.execute(statement)).all()}


async def skipped_refs(
    connection: AsyncConnection, user_id: str, *, now: datetime | None = None
) -> frozenset[TitleRef]:
    """Titles this user said "not now" to; inside the cool-down when ``now`` is given.

    Skips are kept out of the vote history — they teach nothing, so nothing should read
    them as an opinion — which means the only thing that can keep a skipped title out of
    the next batch is this set. Past the cool-down it stops being returned and the title
    becomes a candidate again, exactly as the architecture says.

    Without ``now`` it answers the other half of the same question: *every* title this
    user skipped, whatever its age. The context builder needs both, because a skipped
    card keeps its row for ever and would otherwise stay in the "already shown" list
    long after the cool-down let it back into the pool.
    """
    statement = (
        select(votes.c.kind, votes.c.tmdb_id)
        .where(votes.c.user_id == user_id)
        .where(votes.c.value == "skip")
    )
    if now is not None:
        statement = statement.where(votes.c.voted_at > now - timedelta(days=SKIP_COOL_DOWN))
    return _refs(await connection.execute(statement))


async def list_likes(
    connection: AsyncConnection,
    user_id: str,
    *,
    requested: bool | None = None,
    limit: int = 50,
    before: LikeCursor | None = None,
) -> list[VoteRecord]:
    """Return the titles whose current vote is ``like``, most recent first.

    ``seen_liked`` is not a like here, and that is the contract's wording as well as the
    architecture's: it is a title the person has already watched, so there is nothing to
    request; it feeds the taste profile and appears in no list.

    The cursor is the **whole** sort key, not just the timestamp. One offline-queue
    submission stores every one of its items at the same instant, so a timestamp alone
    would page past all but the first of them: fifty likes flushed together, a page of
    fifty, and the rest unreachable for ever.
    """
    statement = (
        votes.select()
        .where(votes.c.user_id == user_id)
        .where(votes.c.value == "like")
        .order_by(votes.c.voted_at.desc(), votes.c.kind.desc(), votes.c.tmdb_id.desc())
        .limit(limit)
    )
    if requested is True:
        statement = statement.where(votes.c.requested_at.isnot(None))
    if requested is False:
        statement = statement.where(votes.c.requested_at.is_(None))
    if before is not None:
        statement = statement.where(
            tuple_(votes.c.voted_at, votes.c.kind, votes.c.tmdb_id)
            < tuple_(
                literal(before.at, UtcDateTime),
                literal(before.ref.kind),
                literal(before.ref.tmdb_id),
            )
        )
    return _to_votes(await connection.execute(statement))


async def mark_requested(
    connection: AsyncConnection,
    user_id: str,
    ref: TitleRef,
    *,
    status: RequestStatus,
    now: datetime,
) -> None:
    """Record that this title was requested from here, keeping the first time it was."""
    await connection.execute(
        votes.update()
        .where(votes.c.user_id == user_id)
        .where(votes.c.kind == ref.kind)
        .where(votes.c.tmdb_id == ref.tmdb_id)
        .values(requested_at=func.coalesce(votes.c.requested_at, now), request_status=status)
    )


def _one(user_id: str, ref: TitleRef) -> Select[Any]:
    """Select one user's row about one title."""
    return (
        votes.select()
        .where(votes.c.user_id == user_id)
        .where(votes.c.kind == ref.kind)
        .where(votes.c.tmdb_id == ref.tmdb_id)
    )


def _refs(result: Iterable[Row[tuple[Any, ...]]]) -> frozenset[TitleRef]:
    return frozenset(
        TitleRef(kind, row.tmdb_id)
        for row in result
        if (kind := as_media_kind(row.kind)) is not None and row.tmdb_id > 0
    )


def _to_votes(result: Iterable[Row[tuple[Any, ...]]]) -> list[VoteRecord]:
    found = (_to_vote(row) for row in result)
    return [vote for vote in found if vote is not None]


def _to_vote(row: Row[tuple[Any, ...]]) -> VoteRecord | None:
    """Narrow one stored row, or drop it: a database is a file an operator can edit."""
    kind = as_media_kind(row.kind)
    value = as_vote_value(row.value)
    if kind is None or value is None or not isinstance(row.tmdb_id, int) or row.tmdb_id <= 0:
        return None
    return VoteRecord(
        ref=TitleRef(kind, row.tmdb_id),
        value=value,
        card_id=row.card_id,
        pick_type=as_pick_kind(row.pick_type) or "safe",
        title=row.title or "",
        year=row.year,
        poster_path=row.poster_path,
        voted_at=row.voted_at,
        created_at=row.created_at,
        requested_at=row.requested_at,
        request_status=row.request_status,
    )
