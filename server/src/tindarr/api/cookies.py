"""The console's cookies and when they may be set (docs/auth.md, section 2).

Three cookies, all ``HttpOnly``, ``SameSite=Strict``, ``Path=/`` and without ``Domain``:
the web session, the setup session and the pre-auth value that binds console sign-in
handles to a browser (step 2b).

They are normally ``__Host-`` prefixed and ``Secure``, which needs HTTPS. Plain HTTP is
accepted in two cases only: from loopback to ``localhost`` (browsers treat that as a
secure context, so the prefix still works), and with ``TINDARR_ALLOW_HTTP_CONSOLE``
from a private client address, where the cookies drop the prefix and ``Secure``. Any
other plain-HTTP request that would set a cookie gets ``403 https_required``, and the
unprefixed names are only ever read on the requests that may set them.
"""

from dataclasses import dataclass
from typing import Final

from starlette.responses import Response

from tindarr.api.context import RequestContext
from tindarr.auth.errors import https_required
from tindarr.storage.sessions import SessionKind

#: How long a pre-auth cookie lives (step 2b uses it for console sign-in handles).
PREAUTH_MAX_AGE_S: Final = 900
_PATH: Final = "/"
_SAME_SITE: Final = "strict"


@dataclass(frozen=True, slots=True)
class CookieNames:
    """The three cookie names, prefixed or not."""

    session: str
    setup: str
    preauth: str

    def for_kind(self, kind: SessionKind) -> str:
        """Return the cookie carrying a session of this kind."""
        return self.setup if kind == "setup" else self.session


SECURE_NAMES: Final = CookieNames(
    session="__Host-tindarr_session",
    setup="__Host-tindarr_setup",
    preauth="__Host-tindarr_preauth",
)
PLAIN_NAMES: Final = CookieNames(
    session="tindarr_session", setup="tindarr_setup", preauth="tindarr_preauth"
)


@dataclass(frozen=True, slots=True)
class CookieTransport:
    """Which cookie names this request uses, and whether it may set them at all."""

    names: CookieNames
    secure: bool
    allowed: bool

    def require(self) -> "CookieTransport":
        """Return itself, or raise ``https_required`` when cookies may not be set."""
        if not self.allowed:
            raise https_required()
        return self


def cookie_transport(context: RequestContext, *, allow_http_console: bool) -> CookieTransport:
    """Decide how (and whether) this request may carry console cookies."""
    if context.scheme == "https" or context.is_loopback_to_localhost:
        return CookieTransport(SECURE_NAMES, secure=True, allowed=True)
    if allow_http_console and context.client_is_private:
        return CookieTransport(PLAIN_NAMES, secure=False, allowed=True)
    return CookieTransport(SECURE_NAMES, secure=True, allowed=False)


def _set(
    response: Response,
    name: str,
    value: str,
    transport: CookieTransport,
    max_age: int | None = None,
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path=_PATH,
        secure=transport.secure,
        httponly=True,
        samesite=_SAME_SITE,
    )


def set_session_cookie(response: Response, transport: CookieTransport, token: str) -> None:
    """Set the web session cookie: a session cookie, with no ``Max-Age``."""
    _set(response, transport.names.session, token, transport)


def set_setup_cookie(response: Response, transport: CookieTransport, token: str) -> None:
    """Set the setup session cookie."""
    _set(response, transport.names.setup, token, transport)


def set_preauth_cookie(response: Response, transport: CookieTransport, value: str) -> None:
    """Set the pre-auth cookie, which lives fifteen minutes (step 2b)."""
    _set(response, transport.names.preauth, value, transport, PREAUTH_MAX_AGE_S)


def clear_cookie(response: Response, transport: CookieTransport, name: str) -> None:
    """Clear one cookie, with the attributes it was set with."""
    response.delete_cookie(
        name, path=_PATH, secure=transport.secure, httponly=True, samesite=_SAME_SITE
    )


def clear_session_cookie(response: Response, transport: CookieTransport) -> None:
    """Clear the web session cookie (sign-out)."""
    clear_cookie(response, transport, transport.names.session)


def clear_setup_cookie(response: Response, transport: CookieTransport) -> None:
    """Clear the setup session cookie (setup completed)."""
    clear_cookie(response, transport, transport.names.setup)


def clear_preauth_cookie(response: Response, transport: CookieTransport) -> None:
    """Clear the pre-auth cookie (a successful web sign-in)."""
    clear_cookie(response, transport, transport.names.preauth)
