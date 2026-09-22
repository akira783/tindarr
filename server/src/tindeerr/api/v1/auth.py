"""Sign-in, re-authentication, token rotation and sign-out (docs/auth.md, §2, 4, 5, 7).

Every sign-in exists twice: once for the app, which gets a token pair, and once for the
console, which gets the session cookie. The credential check is the same object in both
(``tindeerr.auth.signin``); what differs is only what this layer does with the result,
and that is what these routes are.

The console's half also carries the rules of section 2 that only apply to a request
that **sets** a cookie: no bearer token, HTTPS (or one of the two documented
exceptions), and an ``Origin`` equal to the server's own. While first-run setup is
pending it must also carry the setup session that claimed the server, and that sign-in
is what completes setup.

Handles (Plex PINs, Quick Connect) are created here too, because *who may use them*
depends on how the request was authenticated: a PKCE challenge for the app, the pre-auth
cookie for the console, the session itself for a step-up or an owner token.
"""

from fastapi import APIRouter, Request, Response, status

from tindeerr.api.cookies import (
    CookieTransport,
    clear_preauth_cookie,
    clear_session_cookie,
    clear_setup_cookie,
    set_preauth_cookie,
    set_session_cookie,
)
from tindeerr.api.deps import Services
from tindeerr.api.security import (
    Authenticator,
    Context,
    SharedSession,
    Transport,
    WebOrSetupSession,
    WebSession,
    require_no_bearer,
    require_same_origin,
)
from tindeerr.api.useragent import console_device
from tindeerr.api.v1.models import (
    AuthResultResponse,
    LoginInput,
    PendingResponse,
    PlexLoginInput,
    PlexPinRequest,
    PlexPinResponse,
    PlexPinStatusInput,
    PlexPinStatusResponse,
    QuickConnectLoginInput,
    QuickConnectRequest,
    QuickConnectResponse,
    ReauthInput,
    ReauthResponse,
    RefreshInput,
    TokenPairResponse,
    WebLoginInput,
    WebPlexLoginInput,
    WebQuickConnectLoginInput,
    WebSessionResponse,
)
from tindeerr.auth import access, errors
from tindeerr.auth.handles import Binding, HandlePurpose
from tindeerr.auth.methods import AuthMethod
from tindeerr.auth.sessions import CookieGrant
from tindeerr.auth.signin import Caller, ConsoleSignIn
from tindeerr.auth.tokens import new_token
from tindeerr.core.errors import ProblemError
from tindeerr.ports.media_server import MediaUser
from tindeerr.storage.sessions import NO_DEVICE, Device, Session

router = APIRouter(prefix="/auth", tags=["auth"])

#: The credentials each handle purpose is created under (docs/auth.md, section 5).
_REAUTH_SESSION = Authenticator(cookies=("web",))
_OWNER_TOKEN_SESSION = Authenticator(cookies=("web", "setup"))


# --- the pieces every sign-in route shares ------------------------------------------


def app_caller(context: Context, device: Device) -> Caller:
    """Describe the caller of an app sign-in."""
    return Caller(
        client_key=context.rate_limit_key,
        client_is_private=context.client_is_private,
        device=device,
    )


def console_caller(request: Request, context: Context) -> Caller:
    """Describe the caller of a console sign-in, named after the browser it runs in."""
    return Caller(
        client_key=context.rate_limit_key,
        client_is_private=context.client_is_private,
        console=True,
        device=console_device(request.headers.get("user-agent")),
    )


def guard_cookie_request(request: Request, context: Context, transport: CookieTransport) -> None:
    """Apply the rules of a request that sets a console cookie (docs/auth.md, §2)."""
    require_no_bearer(request)
    transport.require()
    require_same_origin(request, context)


async def setup_session_of(
    request: Request, services: Services, transport: CookieTransport
) -> Session | None:
    """Return the live setup session the browser holds, if it holds one."""
    token = request.cookies.get(transport.names.setup)
    if not token:
        return None
    try:
        return (await services.sessions.authenticate_cookie(token, ("setup",))).session
    except ProblemError:
        # An expired or superseded setup cookie is simply not a setup session.
        return None


async def finish_app_sign_in(
    services: Services, media_user: MediaUser, caller: Caller, method: AuthMethod
) -> AuthResultResponse:
    """Open the app's session and answer with its first token pair."""
    signed_in = await services.sign_in.open_app_session(media_user, caller, method)
    return AuthResultResponse.of(signed_in.grant.tokens, signed_in.user)


async def finish_console_sign_in(
    response: Response,
    services: Services,
    transport: CookieTransport,
    signed_in: ConsoleSignIn,
) -> WebSessionResponse:
    """Set the console's cookies and answer with the new session."""
    set_session_cookie(response, transport, signed_in.grant.token)
    clear_preauth_cookie(response, transport)
    if signed_in.setup_completed_now:
        clear_setup_cookie(response, transport)
    return WebSessionResponse.of(
        signed_in.grant,
        signed_in.user,
        services.clock.now(),
        setup_completed_now=signed_in.setup_completed_now,
    )


# --- password ------------------------------------------------------------------------


@router.post(
    "/login",
    operation_id="login",
    summary="Sign the app in with Jellyfin or Emby credentials",
)
async def login(payload: LoginInput, services: Services, context: Context) -> AuthResultResponse:
    """Check the password with the media server and open an app session."""
    caller = app_caller(context, payload.device.as_device())
    media_user = await services.sign_in.password(payload.username, payload.password, caller)
    return await finish_app_sign_in(services, media_user, caller, "password")


@router.post(
    "/web/login",
    operation_id="webLogin",
    summary="Sign the console in with Jellyfin or Emby credentials",
    tags=["console"],
)
async def web_login(  # noqa: PLR0913, PLR0917 - one per dependency
    payload: WebLoginInput,
    request: Request,
    response: Response,
    services: Services,
    context: Context,
    transport: Transport,
) -> WebSessionResponse:
    """Run the app's check, ending in a web session; complete setup while it is pending."""
    guard_cookie_request(request, context, transport)
    caller = console_caller(request, context)
    media_user = await services.sign_in.password(payload.username, payload.password, caller)
    setup = await setup_session_of(request, services, transport)
    signed_in = await services.sign_in.open_console_session(media_user, caller, "password", setup)
    return await finish_console_sign_in(response, services, transport, signed_in)


# --- Plex PINs ------------------------------------------------------------------------


@router.post(
    "/plex/pins",
    operation_id="createPlexPin",
    summary="Start a Plex PIN (sign-in, re-authentication or owner token)",
    status_code=status.HTTP_201_CREATED,
)
async def create_plex_pin(  # noqa: PLR0913, PLR0917 - one per dependency
    payload: PlexPinRequest,
    request: Request,
    response: Response,
    services: Services,
    context: Context,
    transport: Transport,
) -> PlexPinResponse:
    """Create a plex.tv PIN and hand back a handle bound to whoever asked for it."""
    binding, caller = await resolve_handle_binding(
        payload.purpose, payload.code_challenge, request, response, services, context, transport
    )
    started = await services.plex_pins.create(payload.purpose, binding, caller)
    return PlexPinResponse(
        pin_id=started.handle, auth_url=started.auth_url, expires_at=started.expires_at
    )


@router.post(
    "/plex/pins/status",
    operation_id="getPlexPinStatus",
    summary="Status of an owner-token Plex PIN, without using it",
    tags=["console"],
)
async def plex_pin_status(
    payload: PlexPinStatusInput, services: Services, session: WebOrSetupSession
) -> PlexPinStatusResponse:
    """Tell the wizard whether the owner-token PIN was approved; the handle stays usable."""
    result = await services.plex_pins.status(payload.pin_id, session.session.id)
    return PlexPinStatusResponse(
        status="authorized" if result.authorized else "pending",
        expires_at=result.expires_at,
        account_name=result.account_name,
    )


@router.post(
    "/plex/login",
    operation_id="loginWithPlex",
    summary="Complete a Plex PIN sign-in (app)",
)
async def login_with_plex(
    payload: PlexLoginInput, services: Services, context: Context
) -> AuthResultResponse | PendingResponse:
    """Poll the PIN; once approved, check the account's access and open an app session."""
    caller = app_caller(context, payload.device.as_device())
    media_user = await services.plex_pins.collect(
        payload.pin_id, "sign_in", Binding.verifier(payload.code_verifier)
    )
    return await finish_app_sign_in(services, media_user, caller, "plex_pin")


@router.post(
    "/web/plex/login",
    operation_id="webLoginWithPlex",
    summary="Complete a Plex PIN sign-in (console)",
    tags=["console"],
)
async def web_login_with_plex(  # noqa: PLR0913, PLR0917 - one per dependency
    payload: WebPlexLoginInput,
    request: Request,
    response: Response,
    services: Services,
    context: Context,
    transport: Transport,
) -> WebSessionResponse | PendingResponse:
    """Do the same as the app's, for the browser that started the PIN."""
    guard_cookie_request(request, context, transport)
    caller = console_caller(request, context)
    media_user = await services.plex_pins.collect(
        payload.pin_id, "sign_in", preauth_binding(request, transport)
    )
    setup = await setup_session_of(request, services, transport)
    signed_in = await services.sign_in.open_console_session(media_user, caller, "plex_pin", setup)
    return await finish_console_sign_in(response, services, transport, signed_in)


# --- Quick Connect ----------------------------------------------------------------------


@router.post(
    "/quick-connect",
    operation_id="createQuickConnect",
    summary="Start a Jellyfin Quick Connect sign-in or re-authentication",
    status_code=status.HTTP_201_CREATED,
)
async def create_quick_connect(  # noqa: PLR0913, PLR0917 - one per dependency
    payload: QuickConnectRequest,
    request: Request,
    response: Response,
    services: Services,
    context: Context,
    transport: Transport,
) -> QuickConnectResponse:
    """Ask Jellyfin for a code and hand back a handle bound to whoever asked for it."""
    binding, caller = await resolve_handle_binding(
        payload.purpose, payload.code_challenge, request, response, services, context, transport
    )
    started = await services.quick_connect.create(payload.purpose, binding, caller)
    return QuickConnectResponse(
        handle=started.handle, code=started.code, expires_at=started.expires_at
    )


@router.post(
    "/quick-connect/login",
    operation_id="loginWithQuickConnect",
    summary="Complete a Quick Connect sign-in (app)",
)
async def login_with_quick_connect(
    payload: QuickConnectLoginInput, services: Services, context: Context
) -> AuthResultResponse | PendingResponse:
    """Poll the Quick Connect request; once approved, open an app session."""
    caller = app_caller(context, payload.device.as_device())
    media_user = await services.quick_connect.collect(
        payload.handle, "sign_in", Binding.verifier(payload.code_verifier)
    )
    return await finish_app_sign_in(services, media_user, caller, "quick_connect")


@router.post(
    "/web/quick-connect/login",
    operation_id="webLoginWithQuickConnect",
    summary="Complete a Quick Connect sign-in (console)",
    tags=["console"],
)
async def web_login_with_quick_connect(  # noqa: PLR0913, PLR0917 - one per dependency
    payload: WebQuickConnectLoginInput,
    request: Request,
    response: Response,
    services: Services,
    context: Context,
    transport: Transport,
) -> WebSessionResponse | PendingResponse:
    """Do the same as the app's, for the browser that started the request."""
    guard_cookie_request(request, context, transport)
    caller = console_caller(request, context)
    media_user = await services.quick_connect.collect(
        payload.handle, "sign_in", preauth_binding(request, transport)
    )
    setup = await setup_session_of(request, services, transport)
    signed_in = await services.sign_in.open_console_session(
        media_user, caller, "quick_connect", setup
    )
    return await finish_console_sign_in(response, services, transport, signed_in)


# --- step-up re-authentication ------------------------------------------------------------


@router.post(
    "/web/reauth",
    operation_id="webReauth",
    summary="Re-authenticate on the current media server (step-up)",
    tags=["console"],
)
async def web_reauth(
    payload: ReauthInput,
    services: Services,
    context: Context,
    session: WebSession,
) -> ReauthResponse | PendingResponse:
    """Prove again, on the **current** media server, that the caller is this session's user."""
    user = session.signed_in_user
    caller = Caller(
        client_key=context.rate_limit_key,
        client_is_private=context.client_is_private,
        console=True,
        device=session.session.device,
    )
    media_user = await collect_reauth_proof(payload, services, caller, session.session, user.name)
    expires_at = await services.sign_in.complete_reauth(session.session, user, media_user, caller)
    return ReauthResponse(reauth_expires_at=expires_at)


async def collect_reauth_proof(
    payload: ReauthInput,
    services: Services,
    caller: Caller,
    session: Session,
    user_name: str,
) -> MediaUser:
    """Turn the one proof the body carries into the media server account it proves."""
    binding = Binding.session(session.id)
    if payload.password is not None:
        return await services.sign_in.password(user_name, payload.password, caller)
    if payload.pin_id is not None:
        return await services.plex_pins.collect(payload.pin_id, "reauth", binding)
    return await services.quick_connect.collect(payload.handle or "", "reauth", binding)


# --- sessions --------------------------------------------------------------------------


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


# --- how a handle is bound to the client that created it -----------------------------------


def preauth_binding(request: Request, transport: CookieTransport) -> Binding:
    """Return the binding the pre-auth cookie proves; an absent cookie proves nothing."""
    return Binding.cookie(request.cookies.get(transport.names.preauth) or "")


async def resolve_handle_binding(  # noqa: PLR0913, PLR0917 - one per collaborator of the rule
    purpose: HandlePurpose,
    code_challenge: str | None,
    request: Request,
    response: Response,
    services: Services,
    context: Context,
    transport: CookieTransport,
) -> tuple[Binding, Caller]:
    """Decide who may later use this handle, and under which credentials it is created.

    The table of docs/auth.md, section 5, in code: a PKCE challenge makes it the app's,
    no challenge makes it the console's and it gets the pre-auth cookie, and ``reauth``
    and ``owner_token`` are bound to the session that asked, which must be the right
    kind of session for that purpose.
    """
    require_no_bearer(request)
    if purpose == "sign_in":
        if code_challenge is not None:
            return Binding.pkce(code_challenge), app_caller(context, NO_DEVICE)
        guard_cookie_request(request, context, transport)
        return console_preauth(request, response, transport), console_caller(request, context)
    session = await handle_session(purpose, request, services, context, transport)
    return Binding.session(session.id), console_caller(request, context)


def console_preauth(request: Request, response: Response, transport: CookieTransport) -> Binding:
    """Bind to the browser's pre-auth cookie, setting one when it has none yet."""
    cookie = request.cookies.get(transport.names.preauth)
    if not cookie:
        cookie = new_token()
        set_preauth_cookie(response, transport, cookie)
    return Binding.cookie(cookie)


async def handle_session(
    purpose: HandlePurpose,
    request: Request,
    services: Services,
    context: Context,
    transport: CookieTransport,
) -> Session:
    """Return the session a ``reauth`` or ``owner_token`` handle is created under.

    An owner-token PIN is how the wizard obtains the Plex owner token, so the setup
    session may create one; after setup only a **media server** administrator may, since
    that handle ends up repointing the connector (docs/auth.md, sections 5 and 6).
    """
    authenticator = _REAUTH_SESSION if purpose == "reauth" else _OWNER_TOKEN_SESSION
    credential = await authenticator(request, services, context, transport)
    if purpose == "owner_token" and credential.kind == "web":
        access.require_media_server_admin(credential.signed_in_user)
    if purpose == "reauth" and credential.user is None:  # pragma: no cover - web sessions only
        raise errors.unauthorized()
    return credential.session
