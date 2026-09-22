"""First-run setup endpoints, called by the web console (docs/auth.md, section 3).

The claim opens the one setup session; the state tells the wizard what is left; the
media server step tests the connection before saving it. The sign-in that completes
setup is a web sign-in (step 2b), which calls ``SetupService.complete_setup``.
"""

from fastapi import APIRouter, Response

from tindarr.api.cookies import set_setup_cookie
from tindarr.api.deps import Services
from tindarr.api.security import Context, CookieSetting, SetupSession
from tindarr.api.v1.models import (
    ConnectorStatusResponse,
    LockedValuesResponse,
    MediaServerConfigInput,
    SetupClaimInput,
    SetupStateResponse,
    WebSessionResponse,
    media_server_info,
)
from tindarr.auth.mediaserver import MediaServerInput

router = APIRouter(prefix="/setup", tags=["setup", "console"])


@router.post(
    "/claim",
    operation_id="claimServer",
    summary="Exchange the one-time setup code for a setup session",
)
async def claim_server(
    payload: SetupClaimInput,
    response: Response,
    services: Services,
    context: Context,
    transport: CookieSetting,
) -> WebSessionResponse:
    """Open a setup session for the browser that knows the code, revoking any earlier one."""
    grant = await services.setup.claim(payload.setup_code, client_key=context.rate_limit_key)
    set_setup_cookie(response, transport, grant.token)
    return WebSessionResponse.of(grant, None, services.clock.now())


@router.get(
    "/state",
    operation_id="getSetupState",
    summary="What the setup wizard still has to do",
)
async def get_setup_state(
    services: Services, context: Context, session: SetupSession
) -> SetupStateResponse:
    """Tell the wizard whether the media server is configured, locked, and how to sign in.

    ``session`` is the setup session the wizard holds: it is what authenticates the
    request, and this answer does not depend on it otherwise.
    """
    state = await services.setup.state(client_is_private=context.client_is_private)
    return SetupStateResponse(
        media_server=await media_server_info(services.settings, state.media_server),
        media_server_locked=state.media_server_locked,
        locked_fields=state.locked_fields,
        locked_values=LockedValuesResponse.of(services.connector.locked_values()),
        auth_methods=state.auth_methods,
    )


@router.put(
    "/media-server",
    operation_id="setupMediaServer",
    summary="Configure the media server during setup",
)
async def setup_media_server(
    payload: MediaServerConfigInput, services: Services, session: SetupSession
) -> ConnectorStatusResponse:
    """Test the connection, store it with the server's identity, and report the result."""
    check = await services.setup.configure_media_server(
        session.session,
        MediaServerInput(
            kind=payload.server_type,
            url=payload.url,
            api_key=payload.api_key,
            plex_pin_id=payload.plex_pin_id,
            verify_tls=payload.verify_tls,
            given=frozenset(payload.model_fields_set),
        ),
    )
    return ConnectorStatusResponse.of(check, services.clock.now())
