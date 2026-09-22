"""How a request is authenticated, in FastAPI dependencies (docs/auth.md, section 2).

The rules, in order:

1. an ``Authorization`` header is the only credential considered — every cookie is
   ignored and no CSRF check applies. An endpoint that does not take bearer tokens then
   answers ``401``, exactly as if nothing had been sent;
2. otherwise the cookie matching the endpoint's accepted kinds is looked up; a cookie of
   another kind is never accepted in its place;
3. the session **and** its user are loaded on every request, so a revocation or a
   disable takes effect at once (``tindeerr.auth.sessions``);
4. on ``POST``, ``PUT``, ``PATCH`` and ``DELETE`` authenticated by a cookie, the CSRF
   token must match the session's and ``Origin`` must be the server's own origin;
5. endpoints tagged ``admin`` need the effective role ``admin``;
6. a user whose media server policy forbids remote access is refused from a client
   address that is not private.

The checks are the same for every endpoint: a route declares which credentials it takes
and gets the rest for free.
"""

from dataclasses import dataclass
from typing import Annotated, Final, Literal, NoReturn

from fastapi import Depends, Request

from tindeerr.api.context import RequestContext, request_context
from tindeerr.api.cookies import CookieTransport, cookie_transport
from tindeerr.api.deps import Services
from tindeerr.auth import access, errors
from tindeerr.auth.events import security_event
from tindeerr.auth.sessions import Authenticated
from tindeerr.auth.tokens import tokens_equal
from tindeerr.core.errors import ProblemError
from tindeerr.core.net import normalize_origin
from tindeerr.storage.sessions import Session, SessionKind
from tindeerr.storage.users import User

#: Methods that change state and therefore need a CSRF token and an ``Origin``.
UNSAFE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER: Final = "X-CSRF-Token"
_BEARER_PREFIX: Final = "bearer "


def context_of(request: Request) -> RequestContext:
    """FastAPI dependency returning the request context."""
    return request_context(request.scope)


Context = Annotated[RequestContext, Depends(context_of)]


def transport_of(services: Services, context: Context) -> CookieTransport:
    """FastAPI dependency returning the cookie names and flags this request uses."""
    return cookie_transport(context, allow_http_console=services.config.allow_http_console)


Transport = Annotated[CookieTransport, Depends(transport_of)]


@dataclass(frozen=True, slots=True)
class Credential:
    """An authenticated request: which session, which user, and how it arrived."""

    session: Session
    user: User | None
    via: Literal["bearer", "cookie"]

    @property
    def kind(self) -> SessionKind:
        """The session's kind."""
        return self.session.kind

    @property
    def signed_in_user(self) -> User:
        """The user, for endpoints that always have one."""
        if self.user is None:  # pragma: no cover - only a setup session has no user
            raise errors.unauthorized()
        return self.user


def bearer_token(request: Request) -> str | None:
    """Return the access token of an ``Authorization: Bearer`` header, if there is one."""
    header = request.headers.get("authorization")
    if header is None:
        return None
    if not header.lower().startswith(_BEARER_PREFIX):
        # Any other scheme is still "a credential was sent": never fall back to cookies.
        return ""
    return header[len(_BEARER_PREFIX) :].strip()


def require_no_bearer(request: Request) -> None:
    """Refuse an ``Authorization`` header on a console endpoint (``401``, as if empty)."""
    if bearer_token(request) is not None:
        raise errors.unauthorized()


def require_same_origin(request: Request, context: Context) -> None:
    """Check ``Origin`` against the server's own origin (login CSRF, and every unsafe call)."""
    origin = request.headers.get("origin")
    if origin is None:
        _refuse_csrf("missing_origin")
    if context.origin is None or normalize_origin(origin) != context.origin:
        _refuse_csrf("wrong_origin")


def require_csrf_token(request: Request, session: Session, context: RequestContext) -> None:
    """Check the CSRF token and the ``Origin`` of an unsafe cookie request."""
    sent = request.headers.get(CSRF_HEADER)
    if session.csrf_token is None or sent is None:
        _refuse_csrf("missing_token")
    if not tokens_equal(sent, session.csrf_token):
        _refuse_csrf("wrong_token")
    require_same_origin(request, context)


def _refuse_csrf(reason: str) -> NoReturn:
    security_event("csrf_failed", reason=reason)
    raise errors.csrf_failed()


def cookie_setting_request(
    request: Request, context: Context, transport: Transport
) -> CookieTransport:
    """Guard the endpoints that open a session: no bearer, HTTPS, and a matching ``Origin``.

    Used by the setup claim and, from step 2b, by the web sign-ins.
    """
    require_no_bearer(request)
    transport.require()
    require_same_origin(request, context)
    return transport


CookieSetting = Annotated[CookieTransport, Depends(cookie_setting_request)]


class Authenticator:
    """Resolves the credential of a request for one set of accepted kinds."""

    def __init__(
        self,
        *,
        bearer: bool = False,
        cookies: tuple[SessionKind, ...] = (),
        admin: bool = False,
    ) -> None:
        self._bearer = bearer
        self._cookies = cookies
        self._admin = admin

    async def __call__(
        self, request: Request, services: Services, context: Context, transport: Transport
    ) -> Credential:
        """Authenticate the request, or raise the matching problem."""
        token = bearer_token(request)
        if token is not None:
            if not self._bearer or not token:
                raise errors.unauthorized()
            authenticated = await services.sessions.authenticate_access_token(token)
            credential = Credential(authenticated.session, authenticated.user, "bearer")
        else:
            authenticated = await self._from_cookie(request, services, transport)
            credential = Credential(authenticated.session, authenticated.user, "cookie")
            if request.method in UNSAFE_METHODS:
                require_csrf_token(request, credential.session, context)
        self._authorize(credential, context)
        return credential

    async def _from_cookie(
        self, request: Request, services: Services, transport: CookieTransport
    ) -> Authenticated:
        problem = errors.unauthorized()
        for kind in self._cookies:
            token = request.cookies.get(transport.names.for_kind(kind))
            if not token:
                continue
            try:
                return await services.sessions.authenticate_cookie(token, (kind,))
            except ProblemError as failure:
                # A cookie that no longer works does not hide another kind's.
                problem = failure
        raise problem

    def _authorize(self, credential: Credential, context: RequestContext) -> None:
        if credential.user is None:
            return
        if self._admin:
            access.require_admin(credential.user)
        access.require_remote_access(credential.user, client_is_private=context.client_is_private)


#: Shared endpoints: the app's bearer token or the console's session cookie.
SharedSession = Annotated[Credential, Depends(Authenticator(bearer=True, cookies=("web",)))]
#: Console endpoints: a web session only (a bearer token gets ``401``).
WebSession = Annotated[Credential, Depends(Authenticator(cookies=("web",)))]
#: Console endpoints reserved to admins.
AdminSession = Annotated[Credential, Depends(Authenticator(cookies=("web",), admin=True))]
#: Setup endpoints: the setup session only.
SetupSession = Annotated[Credential, Depends(Authenticator(cookies=("setup",)))]
#: ``GET /auth/web/session``: whichever console session the browser holds.
WebOrSetupSession = Annotated[Credential, Depends(Authenticator(cookies=("web", "setup")))]
