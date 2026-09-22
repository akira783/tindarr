"""The single ``server_state`` row: instance-wide facts that are not settings."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from tindeerr.storage.tables import server_state


class ServerStateRepository:
    """Access to the ``server_state`` row created by the first migration."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def setup_completed(self) -> bool:
        """Whether first-run setup has been completed (it is completed in step 2)."""
        async with self._engine.connect() as connection:
            completed_at = await connection.scalar(
                select(server_state.c.setup_completed_at).where(server_state.c.id == 1)
            )
        return completed_at is not None
