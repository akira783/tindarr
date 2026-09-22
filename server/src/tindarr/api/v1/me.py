"""The signed-in user's own account and devices (docs/auth.md, sections 2 and 7).

Reading is open to both clients: the app and the console both ask who is signed in and
which devices are connected. Changing is console only, and for one reason each:

- **revoking a session** is how a lost phone is cut off. Letting that same phone's
  token do it would let whoever took it revoke the owner's console session instead;
- **deleting the account's data** is irreversible, so it belongs on the screen where
  the user is signed in with their media server credentials, behind a CSRF token and
  an explicit ``confirm`` in the query.

A session id is not a credential: it names a row the caller must already own. The
lookup is scoped to the caller, so another user's session id is a ``404``.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Response, status
from pydantic import BaseModel

from tindarr.api.cookies import clear_session_cookie
from tindarr.api.deps import Services
from tindarr.api.security import SharedSession, Transport, WebSession
from tindarr.api.v1.models import SessionResponse, UserResponse
from tindarr.auth import errors
from tindarr.auth.events import security_event
from tindarr.auth.users import delete_user
from tindarr.storage import sessions as session_repository

router = APIRouter(prefix="/me", tags=["me"])

SessionId = Annotated[str, Path(description="The session to revoke.", max_length=64)]
#: Deleting everything is not something a stray click does.
DeleteConfirmation = Annotated[
    Literal["delete-my-data"], Query(description="Must be `delete-my-data`.")
]


class SessionListResponse(BaseModel):
    """Answer of ``GET /me/sessions``."""

    sessions: list[SessionResponse]


@router.get("", operation_id="getMe", summary="The signed-in user")
async def get_me(session: SharedSession) -> UserResponse:
    """Return the caller, as both clients show them."""
    return UserResponse.of(session.signed_in_user)


@router.get("/sessions", operation_id="listMySessions", summary="The caller's sessions")
async def list_my_sessions(services: Services, session: SharedSession) -> SessionListResponse:
    """Return the caller's live sessions, most recent first, marking the current one."""
    async with services.engine.connect() as connection:
        rows = await session_repository.list_for_user(connection, session.signed_in_user.id)
    return SessionListResponse(
        sessions=[
            SessionResponse.of(row, current=row.id == session.session.id)
            for row in rows
            if row.kind != "setup"
        ]
    )


@router.delete(
    "/sessions/{session_id}",
    operation_id="revokeMySession",
    summary="Revoke one of the caller's sessions",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["console"],
)
async def revoke_my_session(
    session_id: SessionId,
    response: Response,
    services: Services,
    transport: Transport,
    session: WebSession,
) -> None:
    """Sign one of the caller's devices out; revoking their own clears its cookie."""
    async with services.engine.connect() as connection:
        target = await session_repository.get(connection, session_id)
    if target is None or target.user_id != session.signed_in_user.id:
        raise errors.not_found("No such session.")
    await services.sessions.revoke(target.id, "user")
    if target.id == session.session.id:
        clear_session_cookie(response, transport)


@router.delete(
    "",
    operation_id="deleteMyData",
    summary="Delete all of the caller's Tindarr data and sign out everywhere",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["console"],
)
async def delete_my_data(
    confirm: DeleteConfirmation,
    response: Response,
    services: Services,
    transport: Transport,
    session: WebSession,
) -> None:
    """Remove the caller's user row, and with it every session, token and pairing.

    Nothing is deleted on the media server or the request backend. Signing in again
    creates a new, empty account, as a first sign-in does.
    """
    user = session.signed_in_user
    await delete_user(services.engine, user)
    clear_session_cookie(response, transport)
    security_event("user_deleted_their_data", user_id=user.id)
