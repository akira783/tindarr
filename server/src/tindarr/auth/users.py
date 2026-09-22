"""Users: linking them at sign-in, and what an administrator may change (ADR 0010).

Every successful sign-in goes through ``link_user``, whatever the method: it finds the
user by media server id or creates them, refuses a disabled account, and writes back
what the media server just said (name, administrator flag, remote access). The caller
then applies the remote-access rule and opens a session.

``update_user`` is the other half: the console's ``PATCH /admin/users/{id}``, with the
three rules of ADR 0010 — the last enabled admin stays, only a media server
administrator may demote or disable another one, and demoting clears both halves of the
role. They are checked inside the transaction that writes, so two administrators acting
at once cannot both win.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tindarr.auth import errors
from tindarr.auth.events import security_event
from tindarr.ports.media_server import MediaUser
from tindarr.storage import sessions as session_repository
from tindarr.storage import users as repository
from tindarr.storage.db import write_transaction
from tindarr.storage.users import Role, User


async def link_user(connection: AsyncConnection, media_user: MediaUser, now: datetime) -> User:
    """Return the Tindarr user for this media server account, created or refreshed.

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


@dataclass(frozen=True, slots=True)
class UserUpdate:
    """What an administrator asked to change on one user (``PATCH /admin/users/{id}``).

    ``daily_generation_limit`` is nullable, so ``limit_given`` tells "set it to null"
    apart from "leave it alone".
    """

    enabled: bool | None = None
    role: Role | None = None
    daily_generation_limit: int | None = None
    limit_given: bool = False

    @property
    def demotes(self) -> bool:
        """Whether this change takes the admin role away."""
        return self.role == "user"

    @property
    def disables(self) -> bool:
        """Whether this change disables the account."""
        return self.enabled is False


async def list_admin_users(engine: AsyncEngine) -> Sequence[User]:
    """Return every user, oldest first (``GET /admin/users``)."""
    async with engine.connect() as connection:
        return await repository.list_all(connection)


async def get_user(engine: AsyncEngine, user_id: str) -> User:
    """Return one user, or raise ``404 not_found``."""
    async with engine.connect() as connection:
        user = await repository.get(connection, user_id)
    if user is None:
        raise errors.not_found("No such user.")
    return user


async def update_user(
    engine: AsyncEngine,
    *,
    user_id: str,
    update: UserUpdate,
    by: User,
    now: datetime,
) -> User:
    """Apply an administrator's change, with the rules of ADR 0010.

    Everything happens in one write transaction: the last-admin count, the change
    itself and, when the user is disabled, the revocation of their sessions. Two
    administrators demoting the last two admins at the same time therefore cannot both
    succeed.
    """
    async with write_transaction(engine) as connection:
        user = await repository.get(connection, user_id)
        if user is None:
            raise errors.not_found("No such user.")
        await _check_rules(connection, user, update, by)
        values = _changed_values(user, update)
        if values:
            await repository.update_fields(connection, user.id, **values)
        if update.disables and user.enabled:
            await session_repository.revoke_for_user(connection, user.id, "disabled", now)
        changed = await repository.get(connection, user.id)
    if changed is None:  # pragma: no cover - the row was just written
        raise errors.not_found("No such user.")
    return changed


def require_may_act_on(user: User, by: User) -> None:
    """Refuse a promoted admin acting against a media server administrator.

    Demoting, disabling and signing them out all end the same way — the people who can
    repoint the media server lose the console — so they are refused the same way
    (ADR 0010, docs/auth.md section 7).
    """
    if user.media_server_admin and not by.media_server_admin:
        raise errors.forbidden("Only an administrator of the media server can act on another one.")


async def _check_rules(
    connection: AsyncConnection, user: User, update: UserUpdate, by: User
) -> None:
    """Refuse the changes ADR 0010 reserves or forbids."""
    takes_access_away = (update.demotes and user.is_admin) or (update.disables and user.enabled)
    if not takes_access_away:
        return
    # A promoted admin must not be able to remove the people who can repoint the media
    # server, and so lock the household out of its own console.
    require_may_act_on(user, by)
    if user.is_admin and user.enabled and await repository.count_enabled_admins(connection) <= 1:
        raise errors.last_admin()


def _changed_values(user: User, update: UserUpdate) -> dict[str, object]:
    values: dict[str, object] = {}
    if update.enabled is not None and update.enabled != user.enabled:
        values["enabled"] = update.enabled
        values["disabled_reason"] = None if update.enabled else "admin"
    if update.role == "admin":
        values["promoted"] = True
    elif update.role == "user":
        # Both halves go: the media server's own flag is read again at the next
        # sign-in, so a media server administrator becomes admin again then.
        values["promoted"] = False
        values["media_server_admin"] = False
    if update.limit_given:
        values["daily_generation_limit"] = update.daily_generation_limit
    return values


async def delete_user(engine: AsyncEngine, user: User) -> None:
    """Delete a user and everything that hangs off them (``DELETE /me``).

    Sessions, refresh tokens and pairings go with the row through the foreign keys.
    Nothing is deleted on the media server: signing in again creates a new, empty
    account. The last-admin rule applies here too — an administrator who deleted
    themselves would leave the console with no way in — and it is checked against the
    row as it stands inside the transaction, not the copy the request was authenticated
    with: somebody promoted a moment ago is still the last admin.
    """
    async with write_transaction(engine) as connection:
        current = await repository.get(connection, user.id)
        if current is None:
            return
        if (
            current.is_admin
            and current.enabled
            and await repository.count_enabled_admins(connection) <= 1
        ):
            raise errors.last_admin()
        await repository.delete(connection, current.id)
    security_event("user_deleted", user_id=user.id)
