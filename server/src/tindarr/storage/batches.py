"""The ``batches`` and ``cards`` tables: what was generated, and what is still showable.

[ADR 0007](../../../../docs/adr/0007-server-side-cards.md) is the whole of this module's
reason to exist. In the fork a batch lived in memory and a vote sent the card back as the
client held it — title, genres, poster, pick type — so the server believed whatever it
was told about what it had shown. Here a card is a row: the client gets an opaque id and
nothing else it could forge, and a vote is resolved through that id against **this
user's** rows.

Three rules are enforced here rather than left to a caller, because each of them is one
query away from being forgotten:

- **Every read takes a ``user_id``.** There is no "get card by id"; there is "get this
  user's card by id". Another user's id is a miss, not a row.
- **Serving is what starts the clock.** ``serve`` stamps ``served_at`` and
  ``expires_at`` in one write, so a card cannot be in the "already shown" list without
  an expiry or the other way round — the check constraint says so too.
- **Expiry and purge are different things.** A card leaves the deck 24 h after being
  served, and a vote on it is still accepted long after: a phone that was in a tunnel
  must not lose its queue. Only ``purge`` removes anything, and only a card nobody ever
  voted on, a month after it was shown.

The four enrichment columns are JSON, and they are parsed back through pydantic models
rather than trusted: a poster path that is not a path would be a URL the console loads,
and a trailer key that is not a key would be an attacker's choice of YouTube page.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import ColumnElement, Row, Select, delete, func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncConnection

from tindarr.ports.deck import (
    MediaFilter,
    Novelty,
    PickKind,
    as_media_filter,
    as_novelty,
    as_pick_kind,
)
from tindarr.ports.titles import TitleRef, as_media_kind
from tindarr.storage.ids import new_id
from tindarr.storage.tables import batches, cards, votes

__all__ = [
    "CARD_LIFETIME",
    "CARD_RETENTION",
    "MAX_STORED_PROVIDERS",
    "BatchRecord",
    "NewCard",
    "StoredCard",
    "StoredProvider",
    "StoredRatings",
    "StoredTrailer",
    "count_pending",
    "create_batch",
    "get_card",
    "latest_batch",
    "list_pending",
    "pending_of_batch",
    "purge",
    "ready_batch",
    "serve_batch",
    "served_refs",
    "store_cards",
]

#: How long a served card stays in the deck (ADR 0007). A vote on it is accepted after
#: that: the expiry is about what the deck offers, not about what it will listen to.
CARD_LIFETIME: Final = timedelta(hours=24)
#: How long an **unvoted** served card is kept before it is deleted (ADR 0007). It is the
#: width of the offline queue's window, so it is generous on purpose.
CARD_RETENTION: Final = timedelta(days=30)
#: How many streaming offers one card stores. TMDb lists every rental shop in some
#: regions, and a card shows a row of logos, not a directory.
MAX_STORED_PROVIDERS: Final = 12
#: Longest stored free text (a title, a rationale). The card builder already bounds a
#: rationale; this is the backstop for everything TMDb spells at length.
_MAX_TEXT = 500
#: How many genre names one card keeps. TMDb rarely gives more than three.
_MAX_GENRES = 20
_POSTER_PATTERN = r"^/[A-Za-z0-9._-]{1,128}$"


class StoredProvider(BaseModel):
    """One streaming offer, as TMDb listed it for the server's region.

    ``subscribed`` is deliberately absent: it is not a fact about the title, it is a
    fact about the person looking, and it is computed when the card is served so that
    ticking a service updates cards already in somebody's hand.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    provider_id: int = Field(gt=0)
    name: str = Field(max_length=100)
    offer: Literal["subscription", "free", "ads", "rent", "buy"]
    logo_path: str | None = Field(default=None, pattern=_POSTER_PATTERN)


class StoredRatings(BaseModel):
    """The four ratings a card can show. Every one of them is usually missing."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    tmdb: float | None = Field(default=None, ge=0, le=10)
    imdb: float | None = Field(default=None, ge=0, le=10)
    rotten_tomatoes: int | None = Field(default=None, ge=0, le=100)
    metacritic: int | None = Field(default=None, ge=0, le=100)


class StoredTrailer(BaseModel):
    """A YouTube trailer, as a key rather than a link.

    The pattern is the contract's, and it is the security boundary: the app builds a
    ``youtube-nocookie.com`` URL around this string, so anything that is not a video id
    would be somebody else's choice of page.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    site: Literal["youtube"] = "youtube"
    key: str = Field(pattern=r"^[A-Za-z0-9_-]{6,20}$")
    name: str | None = Field(default=None, max_length=_MAX_TEXT)
    language: str | None = Field(default=None, max_length=16)


@dataclass(frozen=True, slots=True)
class NewCard:
    """One card ready to be written, as the generator built it."""

    ref: TitleRef
    title: str
    pick_type: PickKind = "safe"
    original_title: str | None = None
    year: int | None = None
    overview: str | None = None
    genres: tuple[str, ...] = ()
    runtime_minutes: int | None = None
    seasons: int | None = None
    poster_path: str | None = None
    backdrop_path: str | None = None
    ratings: StoredRatings = field(default_factory=StoredRatings)
    providers: tuple[StoredProvider, ...] = ()
    trailer: StoredTrailer | None = None
    rationale: str | None = None


@dataclass(frozen=True, slots=True)
class StoredCard:
    """One row of ``cards``, with its JSON columns already narrowed."""

    id: str
    batch_id: str
    user_id: str
    ref: TitleRef
    pick_type: PickKind
    position: int
    title: str
    original_title: str | None
    year: int | None
    overview: str | None
    genres: tuple[str, ...]
    runtime_minutes: int | None
    seasons: int | None
    poster_path: str | None
    backdrop_path: str | None
    ratings: StoredRatings
    providers: tuple[StoredProvider, ...]
    trailer: StoredTrailer | None
    rationale: str | None
    created_at: datetime
    served_at: datetime | None
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class BatchRecord:
    """One row of ``batches``."""

    id: str
    user_id: str
    mode: Literal["normal", "calibration"]
    novelty: Novelty
    media_filter: MediaFilter
    strategy: str
    mood: str | None
    cards_count: int
    created_at: datetime
    served_at: datetime | None


async def create_batch(  # noqa: PLR0913 - one keyword per column the batch is built with
    connection: AsyncConnection,
    user_id: str,
    *,
    mode: Literal["normal", "calibration"],
    novelty: Novelty,
    media_filter: MediaFilter,
    strategy: str,
    mood: str | None,
    now: datetime,
) -> str:
    """Open an empty batch and return its id. The cards are written when they exist."""
    batch_id = new_id()
    await connection.execute(
        batches.insert().values(
            id=batch_id,
            user_id=user_id,
            mode=mode,
            novelty=novelty,
            media_filter=media_filter,
            strategy=strategy,
            mood=mood,
            cards_count=0,
            created_at=now,
        )
    )
    return batch_id


async def store_cards(
    connection: AsyncConnection,
    batch_id: str,
    user_id: str,
    new_cards: Sequence[NewCard],
    *,
    now: datetime,
) -> list[str]:
    """Write a batch's cards in the order they were chosen; return their ids.

    The count goes on the batch in the same transaction, because "this batch came back
    empty" is the fact that stops the deck asking for another one, and a count written
    separately is a count that can disagree with the rows.
    """
    ids = [new_id() for _ in new_cards]
    if new_cards:
        await connection.execute(
            cards.insert(),
            [
                {
                    "id": card_id,
                    "batch_id": batch_id,
                    "user_id": user_id,
                    "kind": card.ref.kind,
                    "tmdb_id": card.ref.tmdb_id,
                    "pick_type": card.pick_type,
                    "position": position,
                    "title": card.title[:_MAX_TEXT],
                    "original_title": _cut(card.original_title),
                    "year": card.year,
                    "overview": card.overview,
                    "genres": json.dumps(list(card.genres)),
                    "runtime_minutes": card.runtime_minutes,
                    "seasons": card.seasons,
                    "poster_path": card.poster_path,
                    "backdrop_path": card.backdrop_path,
                    "ratings": card.ratings.model_dump_json(),
                    "providers": json.dumps(
                        [
                            provider.model_dump(mode="json")
                            for provider in card.providers[:MAX_STORED_PROVIDERS]
                        ]
                    ),
                    "trailer": None if card.trailer is None else card.trailer.model_dump_json(),
                    "rationale": _cut(card.rationale),
                    "created_at": now,
                }
                for position, (card_id, card) in enumerate(zip(ids, new_cards, strict=True))
            ],
        )
    await connection.execute(
        update(batches).where(batches.c.id == batch_id).values(cards_count=len(new_cards))
    )
    return ids


async def ready_batch(
    connection: AsyncConnection, user_id: str, media_filter: MediaFilter
) -> BatchRecord | None:
    """Return this user's oldest generated-but-never-served batch for that filter.

    Oldest first, so a batch the warm-up paid for hours ago is spent before one made a
    minute ago: cards go stale, and the household has already been billed for both.
    """
    statement = (
        batches.select()
        .where(batches.c.user_id == user_id)
        .where(batches.c.served_at.is_(None))
        .where(batches.c.cards_count > 0)
        .where(_filter_matches(media_filter))
        .order_by(batches.c.created_at)
        .limit(1)
    )
    row = (await connection.execute(statement)).first()
    return None if row is None else _to_batch(row)


async def latest_batch(
    connection: AsyncConnection, user_id: str, media_filter: MediaFilter
) -> BatchRecord | None:
    """Return the most recent batch built for this user under that filter."""
    statement = (
        batches.select()
        .where(batches.c.user_id == user_id)
        .where(_filter_matches(media_filter))
        .order_by(batches.c.created_at.desc())
        .limit(1)
    )
    row = (await connection.execute(statement)).first()
    return None if row is None else _to_batch(row)


async def serve_batch(
    connection: AsyncConnection, batch_id: str, user_id: str, *, now: datetime
) -> list[StoredCard]:
    """Mark a batch and its unserved cards as shown, and return the cards.

    Scoped to the owner, and idempotent in the direction that matters: a card already
    served keeps its original expiry, so polling the deck twice cannot extend a card's
    life for ever.
    """
    await connection.execute(
        update(batches)
        .where(batches.c.id == batch_id)
        .where(batches.c.user_id == user_id)
        .where(batches.c.served_at.is_(None))
        .values(served_at=now)
    )
    await connection.execute(
        update(cards)
        .where(cards.c.batch_id == batch_id)
        .where(cards.c.user_id == user_id)
        .where(cards.c.served_at.is_(None))
        .values(served_at=now, expires_at=now + CARD_LIFETIME)
    )
    return await pending_of_batch(connection, user_id, batch_id, now=now)


async def pending_of_batch(
    connection: AsyncConnection, user_id: str, batch_id: str, *, now: datetime
) -> list[StoredCard]:
    """Return one batch's cards that are still showable, in the order they were chosen."""
    statement = (
        _pending(user_id, now).where(cards.c.batch_id == batch_id).order_by(cards.c.position)
    )
    return _to_cards(await connection.execute(statement))


async def list_pending(
    connection: AsyncConnection,
    user_id: str,
    media_filter: MediaFilter,
    *,
    now: datetime,
    limit: int = 50,
) -> list[StoredCard]:
    """Return every card this user can still be shown, oldest batch first.

    "Still showable" is served, not voted on, and not past its expiry. A card that was
    generated but never served is not in here: it belongs to a batch nobody has asked
    for yet, and handing it over out of order would mark it shown without its batch.
    """
    statement = (
        _pending(user_id, now)
        .where(_card_kind_matches(media_filter))
        .order_by(cards.c.created_at, cards.c.position)
        .limit(limit)
    )
    return _to_cards(await connection.execute(statement))


async def count_pending(
    connection: AsyncConnection, user_id: str, media_filter: MediaFilter, *, now: datetime
) -> int:
    """How many cards this user could still be shown under that filter."""
    statement = select(func.count()).select_from(
        _pending(user_id, now).where(_card_kind_matches(media_filter)).subquery()
    )
    return int((await connection.execute(statement)).scalar_one())


async def get_card(connection: AsyncConnection, user_id: str, card_id: str) -> StoredCard | None:
    """Return one of **this user's** cards, whatever its age.

    Expired and unvoted-on are not conditions here, and that is the point: ADR 0007 says
    a vote stays acceptable after the card has left the deck, because the alternative is
    a phone losing the queue it built on a train.
    """
    statement = cards.select().where(cards.c.user_id == user_id).where(cards.c.id == card_id)
    found = _to_cards(await connection.execute(statement))
    return found[0] if found else None


async def served_refs(connection: AsyncConnection, user_id: str) -> frozenset[TitleRef]:
    """Every title this user has already been shown a card for.

    The "already shown" list of the next prompt (ADR 0007). It is read from the rows
    that still exist, so it forgets an unvoted card a month after it was shown — which
    is the same window the purge works to, and is deliberate: a title nobody answered
    about, a month ago, is a title worth offering again.
    """
    statement = (
        select(cards.c.kind, cards.c.tmdb_id)
        .where(cards.c.user_id == user_id)
        .where(cards.c.served_at.isnot(None))
        .distinct()
    )
    found = (await connection.execute(statement)).all()
    return frozenset(
        TitleRef(kind, row.tmdb_id)
        for row in found
        if (kind := as_media_kind(row.kind)) is not None and row.tmdb_id > 0
    )


async def purge(connection: AsyncConnection, *, now: datetime) -> int:
    """Delete served cards nobody voted on, a month after they were shown.

    A voted-on card is kept: the vote points at it, and the pick type on that row is
    what a statistic is counted from. A card of a batch that was never served is kept
    too — it has not been shown, so it has not expired; the batch it belongs to is what
    the deck will spend next.
    """
    voted = select(votes.c.card_id).where(votes.c.card_id.isnot(None))
    result = await connection.execute(
        delete(cards)
        .where(cards.c.served_at.isnot(None))
        .where(cards.c.served_at < now - CARD_RETENTION)
        .where(cards.c.id.notin_(voted))
    )
    return result.rowcount


def _pending(user_id: str, now: datetime) -> Select[Any]:
    """Select the served, unvoted cards of one user that are still inside their day."""
    voted = (
        select(votes.c.card_id).where(votes.c.user_id == user_id).where(votes.c.card_id.isnot(None))
    )
    return (
        cards.select()
        .where(cards.c.user_id == user_id)
        .where(cards.c.served_at.isnot(None))
        .where(cards.c.expires_at > now)
        .where(cards.c.id.notin_(voted))
    )


def _filter_matches(media_filter: MediaFilter) -> ColumnElement[bool]:
    """Match the batches a request for ``media_filter`` may spend.

    A batch built for ``both`` counts for a narrowed request too. Narrowing the media
    type is a switch somebody flicks between two cards, and a batch of ten that cost a
    model call is not something to throw away over it: the cards of the wrong kind are
    simply not listed (``_card_kind_matches``).
    """
    if media_filter == "both":
        return batches.c.media_filter == "both"
    return or_(batches.c.media_filter == media_filter, batches.c.media_filter == "both")


def _card_kind_matches(media_filter: MediaFilter) -> ColumnElement[bool]:
    """Match the cards a narrowed request may see. ``both`` sees them all."""
    return cards.c.kind == media_filter if media_filter in ("movie", "tv") else true()


def _cut(text: str | None) -> str | None:
    return None if text is None else text[:_MAX_TEXT]


def _to_batch(row: Row[tuple[Any, ...]]) -> BatchRecord:
    return BatchRecord(
        id=row.id,
        user_id=row.user_id,
        mode="calibration" if row.mode == "calibration" else "normal",
        novelty=as_novelty(row.novelty) or "balanced",
        media_filter=as_media_filter(row.media_filter) or "both",
        strategy=row.strategy,
        mood=row.mood,
        cards_count=row.cards_count,
        created_at=row.created_at,
        served_at=row.served_at,
    )


def _to_cards(result: Iterable[Row[tuple[Any, ...]]]) -> list[StoredCard]:
    found = (_to_card(row) for row in result)
    return [card for card in found if card is not None]


def _to_card(row: Row[tuple[Any, ...]]) -> StoredCard | None:
    """Narrow one stored row, or drop it: a database is a file an operator can edit."""
    kind = as_media_kind(row.kind)
    pick = as_pick_kind(row.pick_type)
    if kind is None or pick is None or not isinstance(row.tmdb_id, int) or row.tmdb_id <= 0:
        return None
    return StoredCard(
        id=row.id,
        batch_id=row.batch_id,
        user_id=row.user_id,
        ref=TitleRef(kind, row.tmdb_id),
        pick_type=pick,
        position=row.position,
        title=row.title,
        original_title=row.original_title,
        year=row.year,
        overview=row.overview,
        genres=_genres(row.genres),
        runtime_minutes=row.runtime_minutes,
        seasons=row.seasons,
        poster_path=row.poster_path,
        backdrop_path=row.backdrop_path,
        ratings=_model(StoredRatings, row.ratings) or StoredRatings(),
        providers=_providers(row.providers),
        trailer=None if row.trailer is None else _model(StoredTrailer, row.trailer),
        rationale=row.rationale,
        created_at=row.created_at,
        served_at=row.served_at,
        expires_at=row.expires_at,
    )


def _genres(raw: object) -> tuple[str, ...]:
    found = _list(raw)
    return tuple(str(name)[:_MAX_TEXT] for name in found[:_MAX_GENRES])


def _providers(raw: object) -> tuple[StoredProvider, ...]:
    built = (_provider(entry) for entry in _list(raw)[:MAX_STORED_PROVIDERS])
    return tuple(provider for provider in built if provider is not None)


def _list(raw: object) -> list[object]:
    found = _loads(raw)
    return cast("list[object]", found) if isinstance(found, list) else []


def _provider(entry: object) -> StoredProvider | None:
    try:
        return StoredProvider.model_validate(entry)
    except ValidationError:
        return None


def _model[T: BaseModel](schema: type[T], raw: object) -> T | None:
    found = _loads(raw)
    try:
        return schema.model_validate(found)
    except ValidationError:
        return None


def _loads(raw: object) -> object:
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None
