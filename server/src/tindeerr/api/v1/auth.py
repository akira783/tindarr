"""Token rotation, sign-out and the console's session (docs/auth.md, sections 2 and 7).

The sign-in endpoints themselves need a media server adapter and arrive with step 2b;
what is here works without one.
"""

from fastapi import APIRouter, Response, status

from tindeerr.api.cookies import clear_session_cookie, clear_setup_cookie
from tindeerr.api.deps import Services
from tindeerr.api.security import Context, SharedSession, Transport, WebOrSetupSession
from tindeerr.api.v1.models import RefreshInput, TokenPairResponse, WebSessionResponse
from tindeerr.auth.sessions import CookieGrant

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/refresh",
    operation_id="refreshTokens",
    summary="Rotate the refresh token and get a new access token",
)
async def refresh_tokens(
    payload: RefreshInput, services: Services, context: Context
) -> TokenPairResponse:
    """Exchange a refresh token for a new pair; a reused token revokes the session."""
    services.limits.refresh.hit(context.rate_limit_key)
    tokens = await services.sessions.rotate(
        payload.refresh_token, client_is_private=context.client_is_private
    )
    return TokenPairResponse.of(tokens)


@router.post(
    "/logout",
    operation_id="logout",
    summary="Revoke the current session",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def logout(
    response: Response, services: Services, transport: Transport, session: SharedSession
) -> None:
    """Revoke the caller's session; a console session also gets its cookie cleared."""
    await services.sessions.revoke(session.session.id, "logout")
    if session.via == "cookie":
        clear_session_cookie(response, transport)
        clear_setup_cookie(response, transport)


@router.get(
    "/web/session",
    operation_id="getWebSession",
    summary="The current web or setup session, with its CSRF token",
    tags=["console"],
)
async def get_web_session(services: Services, session: WebOrSetupSession) -> WebSessionResponse:
    """Give the console back its CSRF token after a reload, and say who is signed in."""
    # The cookie token itself is never returned: only what the console may keep in memory.
    grant = CookieGrant(session.session, token="")
    return WebSessionResponse.of(grant, session.user, services.clock.now())
