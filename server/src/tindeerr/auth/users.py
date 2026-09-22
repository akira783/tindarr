"""Linking a media server account to a Tindeerr user (docs/auth.md, section 4).

Every successful sign-in goes through ``link_user``, whatever the method: it finds the
user by media server id or creates them, refuses a disabled account, and writes back
what the media server just said (name, administrator flag, remote access). The caller
then applies the remote-access rule and opens a session.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from tindeerr.auth import errors
from tindeerr.auth.events import security_event
from tindeerr.ports.media_server import MediaUser
from tindeerr.storage import users as repository
from tindeerr.storage.users import User


async def link_user(connection: AsyncConnection, media_user: MediaUser, now: datetime) -> User:
    """Return the Tindeerr user for this media server account, created or refreshed.

    Raises ``account_disabled`` for a user an administrator disabled here. A user
    disabled only because the media server had removed them is re-enabled: the media
    server has just accepted them again.
    """
    existing = await repository.get_by_media_server_id(connection, media_user.id)
    if existing is None:
        user = await repository.insert(
            connection,
            repository.new_user(
                media_user.id,
                media_user.name,
                now,
                admin=media_user.is_admin,
                remote=media_user.remote_access,
            ),
        )
        security_event("user_created", user_id=user.id, admin=media_user.is_admin)
        return await _refresh(connection, user, media_user, now)
    if not existing.enabled and existing.disabled_reason != "media_server":
        raise errors.account_disabled()
    return await _refresh(connection, existing, media_user, now)


async def _refresh(
    connection: AsyncConnection, user: User, media_user: MediaUser, now: datetime
) -> User:
    await repository.update_fields(
        connection,
        user.id,
        name=media_user.name,
        media_server_admin=media_user.is_admin,
        remote_access=media_user.remote_access,
        enabled=True,
        disabled_reason=None,
        last_sign_in_at=now,
        last_seen_at=now,
        synced_at=now,
    )
    refreshed = await repository.get(connection, user.id)
    if refreshed is None:  # pragma: no cover - the row was just written
        raise errors.unauthorized()
    return refreshed
