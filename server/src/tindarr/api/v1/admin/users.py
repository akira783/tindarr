"""Users, as an administrator sees and changes them (ADR 0010, docs/auth.md §7).

Roles have two halves, and only one of them is Tindarr's. ``media_server_admin`` is
read from the media server at each sign-in and cleared by the hourly sync; ``promoted``
is the flag an administrator sets here. The effective role is either of them, which
gives the rules ``tindarr.auth.users`` enforces: the last enabled admin stays, only a
media server administrator may demote or disable another one, and demoting clears both
halves until that user's next sign-in.

Everything here is console-only and needs the effective role ``admin``; nothing needs
more, because none of it can repoint the media server.
"""

from typing import Annotated

from fastapi import APIRouter, Path, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tindarr.api.deps import Services
from tindarr.api.security import AdminSession
from tindarr.api.v1.models import AdminUserResponse
from tindarr.auth.events import security_event
from tindarr.auth.users import UserUpdate, get_user, list_admin_users, update_user
from tindarr.storage.users import Role

router = APIRouter(prefix="/users", tags=["admin", "console"])

UserId = Annotated[str, Path(description="The user to act on.", max_length=64)]


class AdminUserListResponse(BaseModel):
    """Answer of ``GET /admin/users``."""

    users: list[AdminUserResponse]


class AdminUserPatchInput(BaseModel):
    """Body of ``PATCH /admin/users/{user_id}``: at least one of the three fields.

    Only ``daily_generation_limit`` may be null (it means "use the server default"),
    so the two others are refused rather than read as "no change".
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    role: Role | None = None
    daily_generation_limit: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _at_least_one_field_and_no_stray_nulls(self) -> "AdminUserPatchInput":
        given = self.model_fields_set
        if not given:
            msg = "send at least one of enabled, role or daily_generation_limit"
            raise ValueError(msg)
        for field in ("enabled", "role"):
            if field in given and getattr(self, field) is None:
                msg = f"{field}: null is not a value for this field"
                raise ValueError(msg)
        return self

    def as_update(self) -> UserUpdate:
        """Turn the body into the change to apply, keeping absent and null apart."""
        return UserUpdate(
            enabled=self.enabled,
            role=self.role,
            daily_generation_limit=self.daily_generation_limit,
            limit_given="daily_generation_limit" in self.model_fields_set,
        )


@router.get("", operation_id="listUsers", summary="Users who signed in at least once")
async def list_users(services: Services, session: AdminSession) -> AdminUserListResponse:
    """Return every user, oldest first."""
    users = await list_admin_users(services.engine)
    return AdminUserListResponse(users=[AdminUserResponse.of(user) for user in users])


@router.patch(
    "/{user_id}",
    operation_id="updateUser",
    summary="Enable, disable, promote or set limits for a user",
)
async def patch_user(
    payload: AdminUserPatchInput, user_id: UserId, services: Services, session: AdminSession
) -> AdminUserResponse:
    """Apply the change, with the last-admin and media-server-administrator rules."""
    changed = await update_user(
        services.engine,
        user_id=user_id,
        update=payload.as_update(),
        by=session.signed_in_user,
        now=services.clock.now(),
    )
    security_event(
        "user_updated",
        user_id=changed.id,
        by=session.signed_in_user.id,
        enabled=changed.enabled,
        role=changed.role,
    )
    return AdminUserResponse.of(changed)


@router.delete(
    "/{user_id}/sessions",
    operation_id="revokeUserSessions",
    summary="Sign a user out of every device",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_user_sessions(user_id: UserId, services: Services, session: AdminSession) -> None:
    """Revoke every session of that user, including the one making this request."""
    user = await get_user(services.engine, user_id)
    await services.sessions.revoke_user_sessions(user.id, "admin")
    security_event("user_sessions_revoked", user_id=user.id, by=session.signed_in_user.id)
