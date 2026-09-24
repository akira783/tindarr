"""The ``watch_history`` table: what one person watched anywhere but this deck.

Everything here is **scoped to one user**, and that is the property worth stating rather
than assuming. An import is a file somebody else's computer wrote; the rows it produces
are keyed on the uploader's id and every read takes a ``user_id``, so a malformed,
hostile or merely wrong import can only ever be wrong about the person who uploaded it.
Nothing written here is shared, cached across accounts, or reachable from another user's
deck. There is no catalogue to poison: a row is a TMDb id and a verdict about one
household's evening.

One row per **source** per title, so a Netflix import and a calibration tick about the
same film sit beside each other and deleting the import leaves the tick standing. The
engine reads them merged (``seen_refs``, ``engagements``), preferring the source that
says the most.
"""

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import Row, delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from tindarr.ports.history import HistorySource, WatchedTitle, as_history_source
from tindarr.ports.media_server import Engagement, EngagementState
from tindarr.ports.titles import TitleRef, as_media_kind
from tindarr.storage.tables import watch_history

__all__ = [
    "answered_refs",
    "delete_source",
    "engagements",
    "list_for_user",
    "record",
    "seen_refs",
]

#: Which state a merged row keeps when two sources disagree: the one that says the most
#: about how far somebody got. A grid tick carries none at all and never wins.
_STATE_RANK: dict[EngagementState | None, int] = {
    None: 0,
    "paused": 1,
    "abandoned": 2,
    "in_progress": 3,
    "mostly_watched": 4,
    "watched": 5,
}


async def record(
    connection: AsyncConnection, user_id: str, rows: Iterable[WatchedTitle], *, now: datetime
) -> int:
    """Write (or replace) this user's rows for the sources they carry; return how many.

    An upsert rather than a delete-then-insert: re-running an import must not empty
    somebody's history for the seconds it takes to fill it again, and a grid answered
    twice keeps the second answer without losing the first wall's other titles.
    """
    values = [
        {
            "user_id": user_id,
            "source": row.source,
            "kind": row.ref.kind,
            "tmdb_id": row.ref.tmdb_id,
            "seen": row.seen,
            "state": row.state,
            "progress": row.progress,
            "episodes_played": row.episodes_played,
            "episodes_total": row.episodes_total,
            "rating": row.rating,
            "last_watched_at": row.last_watched_at,
            "created_at": now,
        }
        for row in rows
    ]
    if not values:
        return 0
    statement = sqlite_insert(watch_history).values(values)
    await connection.execute(
        statement.on_conflict_do_update(
            index_elements=["user_id", "source", "kind", "tmdb_id"],
            set_={
                name: statement.excluded[name]
                for name in (
                    "seen",
                    "state",
                    "progress",
                    "episodes_played",
                    "episodes_total",
                    "rating",
                    "last_watched_at",
                )
            },
        )
    )
    return len(values)


async def list_for_user(connection: AsyncConnection, user_id: str) -> list[WatchedTitle]:
    """Return every row this user has, in a stable order."""
    statement = (
        watch_history.select()
        .where(watch_history.c.user_id == user_id)
        .order_by(watch_history.c.kind, watch_history.c.tmdb_id, watch_history.c.source)
    )
    found = [_to_row(row) for row in (await connection.execute(statement)).all()]
    return [row for row in found if row is not None]


async def seen_refs(connection: AsyncConnection, user_id: str) -> frozenset[TitleRef]:
    """Return the titles this user has said they watched, whatever the source.

    What ``StrategyContext.known`` is built from. A "no, never seen it" from the
    calibration grid is deliberately absent: it keeps a poster off the next wall and it
    must not keep a perfectly good candidate out of the deck.
    """
    return frozenset(row.ref for row in await list_for_user(connection, user_id) if row.seen)


async def answered_refs(connection: AsyncConnection, user_id: str) -> frozenset[TitleRef]:
    """Return every title this user has answered about, "no" included.

    What the calibration grid excludes: a wall must not ask the same question twice,
    whichever way it was answered.
    """
    return frozenset(row.ref for row in await list_for_user(connection, user_id))


async def engagements(connection: AsyncConnection, user_id: str) -> tuple[Engagement, ...]:
    """Return what this user watched, as the engine's own engagement vocabulary.

    One per title rather than one per source: two files that both know about a series
    are one series somebody watched, and the row that says the most about how far they
    got is the one kept.
    """
    best: dict[TitleRef, WatchedTitle] = {}
    for row in await list_for_user(connection, user_id):
        if not row.seen:
            continue
        held = best.get(row.ref)
        if held is None or _rank(row) > _rank(held):
            best[row.ref] = row
    found = (row.as_engagement() for row in best.values())
    return tuple(engagement for engagement in found if engagement is not None)


async def delete_source(connection: AsyncConnection, user_id: str, source: HistorySource) -> int:
    """Forget everything one source told us about this user; return how many rows went.

    This is what undoing an import means. It is deliberately per source and per user:
    removing a Netflix import leaves the calibration answers and the IMDb ratings alone.
    """
    result = await connection.execute(
        delete(watch_history)
        .where(watch_history.c.user_id == user_id)
        .where(watch_history.c.source == source)
    )
    return result.rowcount


def _rank(row: WatchedTitle) -> tuple[int, int]:
    """How much a row says: its state first, then how many episodes it counted."""
    return (_STATE_RANK.get(row.state, 0), row.episodes_played or 0)


def _to_row(row: Row[tuple[Any, ...]]) -> WatchedTitle | None:
    """Narrow one stored row, or drop it: a database is a file an operator can edit."""
    kind = as_media_kind(row.kind)
    source = as_history_source(row.source)
    if kind is None or source is None or not isinstance(row.tmdb_id, int) or row.tmdb_id <= 0:
        return None
    return WatchedTitle(
        ref=TitleRef(kind, row.tmdb_id),
        source=source,
        seen=bool(row.seen),
        state=_as_state(row.state),
        progress=float(row.progress or 0.0),
        episodes_played=row.episodes_played,
        episodes_total=row.episodes_total,
        rating=row.rating,
        last_watched_at=row.last_watched_at,
    )


def _as_state(value: object) -> EngagementState | None:
    return next((state for state in _STATE_RANK if state is not None and state == value), None)
