"""The ``llm_usage`` and ``region_providers`` tables: what a generation cost, and one cache.

``llm_usage`` is two things at once, and it matters which comes first. It is the
administrator's usage page, and it is the counter the **per-user daily cap** is spent
against. A cap counted after the fact is not a cap: the call that broke it has already
been paid for. So ``reserve`` increments the day's count and checks the limit in one
write, before the provider is called, and the tokens are added afterwards when they are
known.

That also decides what a failure costs. A generation that reserved a slot and then found
the provider unreachable keeps the slot, and is counted in ``failures`` as well. The
alternative — refunding on failure — is what turns an AI provider having a bad minute
into an unbounded number of paid attempts, because "it failed" is exactly the state a
retry loop is in.

``region_providers`` is a plain seven-day cache of one TMDb list. It is per region and
not per user, because it is the same list for the whole household and changes about
twice a year.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Row, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from tindarr.storage.tables import llm_usage, region_providers

__all__ = [
    "MAX_CACHED_PROVIDERS",
    "PROVIDER_CACHE_LIFETIME",
    "CachedProviders",
    "DailyLimitReachedError",
    "ProviderOption",
    "UsageRow",
    "count_today",
    "list_usage",
    "read_region_providers",
    "record_failure",
    "record_tokens",
    "reserve",
    "usage_today",
    "write_region_providers",
]

#: How long the region's provider list is kept (the contract says seven days).
PROVIDER_CACHE_LIFETIME: Final = timedelta(days=7)
#: How many providers one region's cache holds. TMDb lists every rental shop going.
MAX_CACHED_PROVIDERS: Final = 200


class ProviderOption(BaseModel):
    """One streaming service a user can say they subscribe to."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    provider_id: int = Field(gt=0)
    name: str = Field(max_length=100)
    logo_path: str | None = Field(default=None, pattern=r"^/[A-Za-z0-9._-]{1,128}$")


@dataclass(frozen=True, slots=True)
class CachedProviders:
    """The region's provider list as it was last read, with when that was."""

    providers: tuple[ProviderOption, ...]
    fetched_at: datetime

    def fresh(self, now: datetime) -> bool:
        """Whether this list is still inside its seven days."""
        return now - self.fetched_at < PROVIDER_CACHE_LIFETIME


@dataclass(frozen=True, slots=True)
class UsageRow:
    """One user's AI usage on one UTC day."""

    day: date
    user_id: str
    generations: int
    input_tokens: int
    output_tokens: int
    failures: int


class DailyLimitReachedError(Exception):
    """The user has spent their generations for the day.

    Not a ``ProblemError``: the storage layer says what happened and the caller decides
    what it is worth. The deck turns it into the contract's ``429 daily_limit_reached``;
    the warm-up simply skips that user without waking anybody up.
    """

    def __init__(self, limit: int) -> None:
        super().__init__("the daily generation limit is reached")
        self.limit = limit


async def reserve(
    connection: AsyncConnection, user_id: str, *, limit: int | None, now: datetime
) -> int:
    """Charge one generation to today and return how many are left, or refuse.

    Must be called inside a write transaction. The read and the increment are one
    decision, and SQLite's write lock is what stops two phones each seeing "one left".

    ``limit`` of ``None`` is "no cap"; ``0`` is a cap of none, which is how an
    administrator switches the engine off for one person without disabling them.
    """
    day = _day(now)
    spent = await _generations(connection, user_id, day)
    if limit is not None and spent >= limit:
        raise DailyLimitReachedError(limit)
    statement = sqlite_insert(llm_usage).values(day=day, user_id=user_id, generations=1)
    await connection.execute(
        statement.on_conflict_do_update(
            index_elements=["day", "user_id"],
            set_={"generations": llm_usage.c.generations + 1},
        )
    )
    return max(limit - spent - 1, 0) if limit is not None else -1


async def record_tokens(
    connection: AsyncConnection,
    user_id: str,
    *,
    input_tokens: int,
    output_tokens: int,
    now: datetime,
) -> None:
    """Add what one call cost to today's row, without touching the reservation."""
    statement = sqlite_insert(llm_usage).values(
        day=_day(now),
        user_id=user_id,
        generations=0,
        input_tokens=max(input_tokens, 0),
        output_tokens=max(output_tokens, 0),
    )
    await connection.execute(
        statement.on_conflict_do_update(
            index_elements=["day", "user_id"],
            set_={
                "input_tokens": llm_usage.c.input_tokens + statement.excluded.input_tokens,
                "output_tokens": llm_usage.c.output_tokens + statement.excluded.output_tokens,
            },
        )
    )


async def record_failure(connection: AsyncConnection, user_id: str, *, now: datetime) -> None:
    """Count one generation that did not produce a batch."""
    statement = sqlite_insert(llm_usage).values(
        day=_day(now), user_id=user_id, generations=0, failures=1
    )
    await connection.execute(
        statement.on_conflict_do_update(
            index_elements=["day", "user_id"], set_={"failures": llm_usage.c.failures + 1}
        )
    )


async def count_today(connection: AsyncConnection, user_id: str, *, now: datetime) -> int:
    """How many generations this user has already spent today."""
    return await _generations(connection, user_id, _day(now))


async def usage_today(connection: AsyncConnection, *, now: datetime) -> dict[str, int]:
    """Today's generation count per user, for the console's user list."""
    statement = select(llm_usage.c.user_id, llm_usage.c.generations).where(
        llm_usage.c.day == _day(now)
    )
    found = (await connection.execute(statement)).all()
    return {str(row.user_id): int(row.generations) for row in found}


async def list_usage(connection: AsyncConnection, *, since: datetime) -> list[UsageRow]:
    """Return every user's usage from that day onwards, most recent day first."""
    statement = (
        llm_usage.select()
        .where(llm_usage.c.day >= _day(since))
        .order_by(llm_usage.c.day.desc(), llm_usage.c.user_id)
    )
    found = (_to_usage(row) for row in (await connection.execute(statement)).all())
    return [row for row in found if row is not None]


async def read_region_providers(connection: AsyncConnection, region: str) -> CachedProviders | None:
    """Return the cached provider list for a region, however old it is.

    Staleness is the caller's decision, not this function's: a list a week old still
    beats a ``502`` when TMDb is down, which is exactly what the contract asks for.
    """
    statement = region_providers.select().where(region_providers.c.region == region)
    row = (await connection.execute(statement)).first()
    if row is None:
        return None
    return CachedProviders(providers=_options(row.providers), fetched_at=row.fetched_at)


async def write_region_providers(
    connection: AsyncConnection, region: str, options: Sequence[ProviderOption], *, now: datetime
) -> None:
    """Cache a region's provider list, in TMDb's own display order."""
    payload = json.dumps(
        [option.model_dump(mode="json") for option in options[:MAX_CACHED_PROVIDERS]]
    )
    statement = sqlite_insert(region_providers).values(
        region=region, providers=payload, fetched_at=now
    )
    await connection.execute(
        statement.on_conflict_do_update(
            index_elements=["region"],
            set_={
                "providers": statement.excluded.providers,
                "fetched_at": statement.excluded.fetched_at,
            },
        )
    )


async def _generations(connection: AsyncConnection, user_id: str, day: str) -> int:
    statement = select(func.coalesce(llm_usage.c.generations, 0)).where(
        llm_usage.c.day == day, llm_usage.c.user_id == user_id
    )
    return int((await connection.execute(statement)).scalar() or 0)


def _day(now: datetime) -> str:
    """Return the UTC day a generation is charged to, as the column stores it."""
    return now.date().isoformat()


def _to_usage(row: Row[tuple[Any, ...]]) -> UsageRow | None:
    try:
        day = date.fromisoformat(str(row.day))
    except ValueError:  # pragma: no cover - written by this module only
        return None
    return UsageRow(
        day=day,
        user_id=str(row.user_id),
        generations=int(row.generations or 0),
        input_tokens=int(row.input_tokens or 0),
        output_tokens=int(row.output_tokens or 0),
        failures=int(row.failures or 0),
    )


def _options(raw: object) -> tuple[ProviderOption, ...]:
    if not isinstance(raw, str):
        return ()
    try:
        found: object = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(found, list):
        return ()
    built = (_option(entry) for entry in cast("list[object]", found)[:MAX_CACHED_PROVIDERS])
    return tuple(option for option in built if option is not None)


def _option(entry: object) -> ProviderOption | None:
    try:
        return ProviderOption.model_validate(entry)
    except ValidationError:
        return None
