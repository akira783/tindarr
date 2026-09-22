"""The hourly reconciliation with the media server (docs/auth.md, section 7).

Sign-ins keep a user's flags fresh, but only for people who sign in. Someone deleted or
disabled on the media server, or who lost their administrator rights there, would
otherwise keep working here until their next sign-in — which may never come, since their
Tindeerr session outlives it. So once an hour Tindeerr asks the media server who its
users are and reconciles.

The rules are deliberately one-way:

- a linked user who is **missing** from the list, or disabled there, is disabled here
  with ``disabled_reason = media_server``, and their sessions are revoked at once. A
  user an administrator has already disabled keeps *that* reason: writing
  ``media_server`` over it would let their next sign-in re-enable them, since a user
  disabled only by the media server is re-enabled when it accepts them again (§4);
- ``media_server_admin`` is **cleared** when the media server no longer says
  administrator. It is never set: only a sign-in grants it, so a demotion done inside
  Tindeerr lasts until that user signs in again (ADR 0010);
- ``name`` and ``remote_access`` follow the media server.

Nothing changes when the media server or plex.tv is unreachable, or when the identity
read first does not match the stored one: the run is abandoned and tried again an hour
later. A sync must never disable a household because a container was restarting.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tindeerr.auth.events import security_event
from tindeerr.auth.mediaserver import MediaServerConnector
from tindeerr.core.clock import Clock
from tindeerr.core.errors import ProblemError
from tindeerr.ports.media_server import MediaUser
from tindeerr.storage import sessions as session_repository
from tindeerr.storage import users as repository
from tindeerr.storage.db import write_transaction
from tindeerr.storage.users import User

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What one run changed."""

    ran: bool = False
    updated: int = 0
    disabled: int = 0
    demoted: int = 0
    revoked: int = 0


class UserSync:
    """Reconciles the Tindeerr users with the media server's."""

    def __init__(
        self,
        engine: AsyncEngine,
        connector: MediaServerConnector,
        clock: Clock,
        install_id: str,
    ) -> None:
        self._engine = engine
        self._connector = connector
        self._clock = clock
        self._install_id = install_id

    async def run(self) -> SyncResult:
        """Reconcile once; do nothing at all when the media server cannot be read."""
        if await self._connector.configured() is None:
            return SyncResult()
        try:
            # ``usable`` checks the server's identity first (docs/auth.md, section 6).
            adapter = await self._connector.usable(self._install_id)
            media_users = await adapter.list_users()
        except ProblemError as problem:
            logger.warning(
                "the media server user sync was skipped", extra={"problem": problem.code}
            )
            return SyncResult()
        if not media_users:
            # A media server with no users at all is not a media server that lost them:
            # it is a credential that can no longer see them. Disabling everyone on that
            # answer would lock the household out of its own server.
            logger.warning("the media server listed no users at all; nothing was changed")
            return SyncResult()
        return await self._reconcile({user.id: user for user in media_users})

    async def _reconcile(self, media_users: dict[str, MediaUser]) -> SyncResult:
        now = self._clock.now()
        updated = disabled = demoted = revoked = 0
        async with write_transaction(self._engine) as connection:
            for user in await repository.list_all(connection):
                if user.media_server_user_id is None:
                    continue
                media_user = media_users.get(user.media_server_user_id)
                if media_user is None or media_user.disabled:
                    if not user.enabled:
                        # Already gone, for this reason or an administrator's: say so
                        # once, not every hour.
                        await repository.update_fields(connection, user.id, synced_at=now)
                        continue
                    revoked += await self._disable(connection, user, now)
                    disabled += 1
                    continue
                demoted += await self._refresh(connection, user, media_user, now)
                updated += 1
        if disabled or demoted:
            security_event(
                "user_sync", disabled=disabled, demoted=demoted, sessions_revoked=revoked
            )
        return SyncResult(
            ran=True,
            updated=updated,
            disabled=disabled,
            demoted=demoted,
            revoked=revoked,
        )

    @staticmethod
    async def _disable(connection: AsyncConnection, user: User, now: datetime) -> int:
        await repository.update_fields(
            connection,
            user.id,
            enabled=False,
            disabled_reason="media_server",
            media_server_admin=False,
            synced_at=now,
        )
        return await session_repository.revoke_for_user(connection, user.id, "disabled", now)

    @staticmethod
    async def _refresh(
        connection: AsyncConnection, user: User, media_user: MediaUser, now: datetime
    ) -> int:
        # Cleared, never set: an administrator here stays one until their next sign-in.
        demoted = user.media_server_admin and not media_user.is_admin
        await repository.update_fields(
            connection,
            user.id,
            name=media_user.name,
            remote_access=media_user.remote_access,
            media_server_admin=user.media_server_admin and media_user.is_admin,
            synced_at=now,
        )
        return int(demoted)
