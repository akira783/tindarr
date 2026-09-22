"""The single ``server_state`` row: instance-wide facts that are not settings.

The install id, the setup state and the media server's identity cannot be forced from
the environment, unlike settings: they describe what this database *is*.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Row, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tindeerr.storage.tables import server_state

ROW_ID = 1


@dataclass(frozen=True, slots=True)
class ServerState:
    """The ``server_state`` row."""

    id: int
    created_at: datetime
    setup_completed_at: datetime | None
    install_id: str
    setup_code_hash: str | None
    media_server_identity: str | None

    @property
    def setup_completed(self) -> bool:
        """Whether first-run setup has been completed."""
        return self.setup_completed_at is not None


def _to_state(row: Row[tuple[Any, ...]]) -> ServerState:
    # Positional: ``ServerState`` lists the columns in order, which a test checks.
    return ServerState(*row)


async def read(connection: AsyncConnection) -> ServerState:
    """Return the single row (created by the first migration)."""
    row = (await connection.execute(server_state.select().where(server_state.c.id == ROW_ID))).one()
    return _to_state(row)


async def set_setup_code_hash(connection: AsyncConnection, code_hash: str | None) -> None:
    """Store (or clear) the SHA-256 of the pending setup code."""
    await connection.execute(
        update(server_state).where(server_state.c.id == ROW_ID).values(setup_code_hash=code_hash)
    )


async def set_media_server_identity(connection: AsyncConnection, identity: str | None) -> None:
    """Store the identity (``jellyfin:<id>``…) of the configured media server."""
    await connection.execute(
        update(server_state)
        .where(server_state.c.id == ROW_ID)
        .values(media_server_identity=identity)
    )


async def complete_setup(connection: AsyncConnection, now: datetime) -> bool:
    """Mark setup completed and clear the code hash; ``False`` if it already was.

    A compare-and-set, so two sign-ins racing to complete setup cannot both win.
    """
    result = await connection.execute(
        update(server_state)
        .where(server_state.c.id == ROW_ID)
        .where(server_state.c.setup_completed_at.is_(None))
        .values(setup_completed_at=now, setup_code_hash=None)
    )
    return result.rowcount == 1


async def reopen_setup(connection: AsyncConnection, code_hash: str) -> None:
    """Put the server back in first-run state with a new setup code (the CLI reset)."""
    await connection.execute(
        update(server_state)
        .where(server_state.c.id == ROW_ID)
        .values(setup_completed_at=None, setup_code_hash=code_hash, media_server_identity=None)
    )


class ServerStateRepository:
    """Read access to the ``server_state`` row for request handling."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def read(self) -> ServerState:
        """Return the current state."""
        async with self._engine.connect() as connection:
            return await read(connection)

    async def setup_completed(self) -> bool:
        """Whether first-run setup has been completed."""
        return (await self.read()).setup_completed
