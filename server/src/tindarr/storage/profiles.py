"""The ``taste_profiles`` and ``preferences`` tables: one row per user, and no defaults stored.

Both tables are absent until somebody changes something, and both are read through a
function that returns a value rather than a row. That is deliberate: a household of six
who never opened the preferences screen has no rows here, and the defaults live in the
code beside the setting they default to rather than being copied into everybody's
database at their first sign-in, where they would silently stop tracking the server's
own language the day an administrator changes it.

The one rule worth stating in the schema's own words: ``user_edited`` is a latch a
rewrite reads and never clears. What somebody wrote about their own taste is not
something a model is allowed to overrule — a rewrite may add to it, and it keeps the
user's sentences intact (roadmap 4.3).
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, cast

from sqlalchemy import Row
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from tindarr.ports.deck import MediaFilter, Novelty, as_media_filter, as_novelty
from tindarr.storage.tables import preferences, taste_profiles

__all__ = [
    "MAX_PROFILE_CHARS",
    "MAX_STREAMING_SERVICES",
    "DeckPreferences",
    "PreferencesPatch",
    "TasteProfile",
    "clear_refresh_error",
    "read_preferences",
    "read_profile",
    "save_generated",
    "save_user_text",
    "set_refresh_error",
    "update_preferences",
]

#: How long a profile may be. The contract's own limit for what a user may type, and the
#: cap a rewrite is truncated to: a model that answers with an essay does not get to fill
#: every later prompt with it.
MAX_PROFILE_CHARS: Final = 4000
#: How many streaming services one person may tick. The contract's ``maxItems``.
MAX_STREAMING_SERVICES: Final = 100


@dataclass(frozen=True, slots=True)
class TasteProfile:
    """One row of ``taste_profiles``: what the person wrote, and what was written for them."""

    #: The last rewrite's bullets. Empty until one has run.
    generated: str
    #: What the user typed. Nothing but a user edit ever writes it.
    user_text: str
    user_edited: bool
    votes_at_update: int
    updated_at: datetime
    refresh_error: str | None = None

    @property
    def text(self) -> str:
        """The whole profile, the person's own words first.

        The order is the guarantee. A prompt reads this top to bottom, and what somebody
        said about their own taste is the first thing it sees — and the only part a
        later rewrite cannot touch, because it lives in another column.
        """
        return "\n\n".join(part for part in (self.user_text, self.generated) if part)


@dataclass(frozen=True, slots=True)
class DeckPreferences:
    """What one user asked the deck for. Absent rows read as these defaults."""

    media_filter: MediaFilter = "both"
    novelty: Novelty = "balanced"
    auto_request: bool = False
    #: ``None`` means "the server's language", read when the deck is built.
    language: str | None = None
    streaming_services: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class PreferencesPatch:
    """A partial change. ``given`` names the fields the request really carried.

    Without it, "do not change the novelty" and "set the novelty to balanced" would be
    the same request, which is the bug every partial-update endpoint writes once.
    """

    media_filter: MediaFilter | None = None
    novelty: Novelty | None = None
    auto_request: bool | None = None
    language: str | None = None
    streaming_services: tuple[int, ...] | None = None
    given: frozenset[str] = field(default_factory=frozenset[str])


async def read_profile(connection: AsyncConnection, user_id: str) -> TasteProfile | None:
    """Return this user's taste profile, or ``None`` before the first one is written."""
    statement = taste_profiles.select().where(taste_profiles.c.user_id == user_id)
    row = (await connection.execute(statement)).first()
    return None if row is None else _to_profile(row)


async def save_generated(
    connection: AsyncConnection, user_id: str, text: str, *, votes_at_update: int, now: datetime
) -> None:
    """Store what a rewrite produced, leaving the user's own words exactly as they are."""
    await _write(
        connection,
        user_id,
        values={"text": text[:MAX_PROFILE_CHARS], "votes_at_update": votes_at_update},
        now=now,
    )


async def save_user_text(
    connection: AsyncConnection, user_id: str, text: str, *, votes_at_update: int, now: datetime
) -> None:
    """Store what the user wrote about their own taste, and latch the edit.

    ``user_edited`` only ever goes up, because the flag says "a person has had their say
    here" — which stays true however many rewrites have since run beside it.
    """
    await _write(
        connection,
        user_id,
        values={
            "user_text": text[:MAX_PROFILE_CHARS],
            "user_edited": True,
            "votes_at_update": votes_at_update,
        },
        now=now,
    )


async def _write(
    connection: AsyncConnection,
    user_id: str,
    *,
    values: dict[str, object],
    now: datetime,
) -> None:
    """Upsert some columns of one profile, clearing the last failure."""
    row: dict[str, object] = {
        "user_id": user_id,
        "text": "",
        "user_text": "",
        "user_edited": False,
        "votes_at_update": 0,
        "updated_at": now,
        "refresh_error": None,
    } | values
    statement = sqlite_insert(taste_profiles).values(row)
    await connection.execute(
        statement.on_conflict_do_update(
            index_elements=["user_id"],
            set_={name: statement.excluded[name] for name in values}
            | {"updated_at": statement.excluded.updated_at, "refresh_error": None},
        )
    )


async def set_refresh_error(
    connection: AsyncConnection, user_id: str, code: str, *, now: datetime
) -> None:
    """Record why the last rewrite failed, without touching the text it did not write."""
    statement = sqlite_insert(taste_profiles).values(
        user_id=user_id,
        text="",
        user_text="",
        user_edited=False,
        votes_at_update=0,
        updated_at=now,
        refresh_error=code,
    )
    await connection.execute(
        statement.on_conflict_do_update(
            index_elements=["user_id"], set_={"refresh_error": statement.excluded.refresh_error}
        )
    )


async def clear_refresh_error(connection: AsyncConnection, user_id: str) -> None:
    """Forget the last rewrite's failure (a new one started, or one succeeded)."""
    await connection.execute(
        taste_profiles.update()
        .where(taste_profiles.c.user_id == user_id)
        .values(refresh_error=None)
    )


async def read_preferences(connection: AsyncConnection, user_id: str) -> DeckPreferences:
    """Return this user's preferences, or the defaults when they have never set any."""
    statement = preferences.select().where(preferences.c.user_id == user_id)
    row = (await connection.execute(statement)).first()
    return DeckPreferences() if row is None else _to_preferences(row)


async def update_preferences(
    connection: AsyncConnection, user_id: str, patch: PreferencesPatch, *, now: datetime
) -> DeckPreferences:
    """Apply a partial change and return the whole of what the deck will now read."""
    current = await read_preferences(connection, user_id)
    wanted = DeckPreferences(
        media_filter=_field(patch, "media_filter", patch.media_filter, current.media_filter),
        novelty=_field(patch, "novelty", patch.novelty, current.novelty),
        auto_request=_field(patch, "auto_request", patch.auto_request, current.auto_request),
        language=patch.language if "language" in patch.given else current.language,
        streaming_services=_services(
            patch.streaming_services if "streaming_services" in patch.given else None,
            current.streaming_services,
        ),
    )
    statement = sqlite_insert(preferences).values(
        user_id=user_id,
        media_filter=wanted.media_filter,
        novelty=wanted.novelty,
        auto_request=wanted.auto_request,
        language=wanted.language,
        streaming_services=json.dumps(list(wanted.streaming_services)),
        updated_at=now,
    )
    await connection.execute(
        statement.on_conflict_do_update(
            index_elements=["user_id"],
            set_={
                name: statement.excluded[name]
                for name in (
                    "media_filter",
                    "novelty",
                    "auto_request",
                    "language",
                    "streaming_services",
                    "updated_at",
                )
            },
        )
    )
    return wanted


def _field[T](patch: PreferencesPatch, name: str, given: T | None, current: T) -> T:
    return given if name in patch.given and given is not None else current


def _services(given: Sequence[int] | None, current: tuple[int, ...]) -> tuple[int, ...]:
    if given is None:
        return current
    kept = sorted({value for value in given if value > 0})
    return tuple(kept[:MAX_STREAMING_SERVICES])


def _to_profile(row: Row[tuple[Any, ...]]) -> TasteProfile:
    return TasteProfile(
        generated=row.text or "",
        user_text=row.user_text or "",
        user_edited=bool(row.user_edited),
        votes_at_update=int(row.votes_at_update or 0),
        updated_at=row.updated_at,
        refresh_error=row.refresh_error,
    )


def _to_preferences(row: Row[tuple[Any, ...]]) -> DeckPreferences:
    """Narrow one stored row: a database is a file an operator can edit."""
    return DeckPreferences(
        media_filter=as_media_filter(row.media_filter) or "both",
        novelty=as_novelty(row.novelty) or "balanced",
        auto_request=bool(row.auto_request),
        language=row.language if isinstance(row.language, str) and row.language else None,
        streaming_services=_stored_services(row.streaming_services),
    )


def _stored_services(raw: object) -> tuple[int, ...]:
    if not isinstance(raw, str):
        return ()
    try:
        found: object = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(found, list):
        return ()
    values = cast("list[object]", found)[:MAX_STREAMING_SERVICES]
    return tuple(value for value in values if isinstance(value, int) and value > 0)
