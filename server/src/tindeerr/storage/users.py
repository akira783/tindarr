"""The ``users`` table: rows and the queries auth runs on them.

Every function takes the connection to run on, so a caller can do several of them in one
transaction (``tindeerr.storage.db.write_transaction``): creating a user and opening a
session, or unlinking everyone and revoking every session, either happen together or not
at all.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Row, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from tindeerr.storage.ids import new_id
from tindeerr.storage.tables import users

__all__ = [
    "DisabledReason",
    "Role",
    "User",
    "count_enabled_admins",
    "get",
    "get_by_media_server_id",
    "insert",
    "list_all",
    "new_user",
    "touch",
    "unlink_all",
    "update_fields",
]

type DisabledReason = Literal["admin", "media_server", "unlinked"]
type Role = Literal["admin", "user"]


@dataclass(frozen=True, slots=True)
class User:
    """One row of ``users``."""

    id: str
    media_server_user_id: str | None
    name: str
    media_server_admin: bool
    promoted: bool
    remote_access: bool
    enabled: bool
    disabled_reason: DisabledReason | None
    daily_generation_limit: int | None
    created_at: datetime
    last_sign_in_at: datetime | None
    last_seen_at: datetime | None
    synced_at: datetime | None

    @property
    def role(self) -> Role:
        """Effective role: ``admin`` when the media server says so, or after a promotion."""
        return "admin" if self.media_server_admin or self.promoted else "user"

    @property
    def is_admin(self) -> bool:
        """Whether the effective role is ``admin`` (docs/adr/0010)."""
        return self.role == "admin"

    @property
    def linked(self) -> bool:
        """Whether the user still points at a media server account."""
        return self.media_server_user_id is not None

    @property
    def can_sign_in(self) -> bool:
        """Whether requests from this user are accepted at all."""
        return self.enabled and self.linked


def _to_user(row: Row[tuple[Any, ...]]) -> User:
    # Positional: ``User`` lists the columns of ``users`` in order, which a test checks.
    return User(*row)


async def get(connection: AsyncConnection, user_id: str) -> User | None:
    """Return the user with this id."""
    row = (await connection.execute(users.select().where(users.c.id == user_id))).one_or_none()
    return None if row is None else _to_user(row)


async def get_by_media_server_id(connection: AsyncConnection, media_id: str) -> User | None:
    """Return the user linked to this media server account."""
    row = (
        await connection.execute(users.select().where(users.c.media_server_user_id == media_id))
    ).one_or_none()
    return None if row is None else _to_user(row)


async def list_all(connection: AsyncConnection) -> Sequence[User]:
    """Return every user, oldest first."""
    rows = (await connection.execute(users.select().order_by(users.c.created_at))).all()
    return [_to_user(row) for row in rows]


def new_user(media_user_id: str, name: str, now: datetime, *, admin: bool, remote: bool) -> User:
    """Build a user row linked to a media server account (not stored yet)."""
    return User(
        id=new_id(),
        media_server_user_id=media_user_id,
        name=name,
        media_server_admin=admin,
        promoted=False,
        remote_access=remote,
        enabled=True,
        disabled_reason=None,
        daily_generation_limit=None,
        created_at=now,
        last_sign_in_at=None,
        last_seen_at=None,
        synced_at=None,
    )


async def insert(connection: AsyncConnection, user: User) -> User:
    """Store a new user row and return it."""
    await connection.execute(users.insert().values(**asdict(user)))
    return user


async def update_fields(connection: AsyncConnection, user_id: str, **values: object) -> None:
    """Write ``values`` on one user row."""
    await connection.execute(users.update().where(users.c.id == user_id).values(**values))


async def touch(connection: AsyncConnection, user_id: str, now: datetime) -> None:
    """Record that the user was seen now."""
    await update_fields(connection, user_id, last_seen_at=now)


async def count_enabled_admins(connection: AsyncConnection) -> int:
    """Return the number of enabled admins, for the last-admin rule (docs/adr/0010)."""
    statement = (
        select(users.c.id)
        .where(users.c.enabled)
        .where(users.c.media_server_admin | users.c.promoted)
    )
    return len((await connection.execute(statement)).all())


async def unlink_all(connection: AsyncConnection) -> int:
    """Unlink every user from the media server, disabling them (identity change, reset).

    Their data is kept; they cannot sign in until an account is linked again, which v1
    does not do.
    """
    result = await connection.execute(
        update(users).values(media_server_user_id=None, enabled=False, disabled_reason="unlinked")
    )
    return result.rowcount
