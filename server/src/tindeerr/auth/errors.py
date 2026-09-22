"""The problems auth raises, with the contract's ``code`` values.

One function per code, so the same status and wording is used everywhere and the API
layer never invents one. Details never name a user, a code or a token: the answer to a
wrong credential must not tell the caller which part was wrong.
"""

from http import HTTPStatus

from tindeerr.core.errors import ProblemError


def unauthorized(detail: str = "No valid credential for this request.") -> ProblemError:
    """401: no credential this operation accepts, or a session that no longer works."""
    return ProblemError(HTTPStatus.UNAUTHORIZED, "unauthorized", detail)


def token_expired() -> ProblemError:
    """401: the access token is past ``exp``; refresh (single-flight) and retry."""
    return ProblemError(HTTPStatus.UNAUTHORIZED, "token_expired", "The access token has expired.")


def refresh_token_reused() -> ProblemError:
    """401: a refresh token came back twice; the session is revoked (docs/adr/0010)."""
    return ProblemError(
        HTTPStatus.UNAUTHORIZED,
        "refresh_token_reused",
        "This refresh token was already used; the session has been revoked.",
    )


def invalid_setup_code() -> ProblemError:
    """401: the setup code does not match."""
    return ProblemError(HTTPStatus.UNAUTHORIZED, "invalid_setup_code", "Wrong setup code.")


def csrf_failed(detail: str = "Missing or invalid CSRF token or Origin.") -> ProblemError:
    """403: the CSRF token or the ``Origin`` of an unsafe cookie request is wrong."""
    return ProblemError(HTTPStatus.FORBIDDEN, "csrf_failed", detail)


def https_required() -> ProblemError:
    """403: console sessions need HTTPS (docs/auth.md, section 2)."""
    return ProblemError(
        HTTPStatus.FORBIDDEN,
        "https_required",
        "The console needs HTTPS, except on localhost or with TINDEERR_ALLOW_HTTP_CONSOLE "
        "from a private address.",
    )


def admin_required() -> ProblemError:
    """403: the caller's effective role is not ``admin``."""
    return ProblemError(HTTPStatus.FORBIDDEN, "admin_required", "Administrators only.")


def media_server_admin_required() -> ProblemError:
    """403: reserved to an administrator of the media server itself."""
    return ProblemError(
        HTTPStatus.FORBIDDEN,
        "media_server_admin_required",
        "Only an administrator of the media server can do this.",
    )


def reauth_required() -> ProblemError:
    """403: this action needs a re-authentication less than five minutes old."""
    return ProblemError(
        HTTPStatus.FORBIDDEN,
        "reauth_required",
        "Re-authenticate on the media server first.",
    )


def remote_access_denied() -> ProblemError:
    """403: the user's media server policy forbids remote access from this address."""
    return ProblemError(
        HTTPStatus.FORBIDDEN,
        "remote_access_denied",
        "Your media server account may only be used from the local network.",
    )


def account_disabled() -> ProblemError:
    """403: the account is disabled, here or on the media server."""
    return ProblemError(HTTPStatus.FORBIDDEN, "account_disabled", "This account is disabled.")


def setup_session_required() -> ProblemError:
    """403: only the browser holding the setup session can complete setup."""
    return ProblemError(
        HTTPStatus.FORBIDDEN,
        "setup_session_required",
        "Start again from the setup code in the console.",
    )


def host_not_allowed() -> ProblemError:
    """400: the ``Host`` header is not one this server answers to."""
    return ProblemError(
        HTTPStatus.BAD_REQUEST,
        "host_not_allowed",
        "This server does not answer to that host name; see TINDEERR_ALLOWED_HOSTS.",
    )


def setup_completed() -> ProblemError:
    """409: first-run setup is already done."""
    return ProblemError(
        HTTPStatus.CONFLICT, "setup_completed", "This server has already been set up."
    )


def secret_required() -> ProblemError:
    """409: the stored secret cannot be reused because the URL or the kind changed."""
    return ProblemError(
        HTTPStatus.CONFLICT,
        "secret_required",
        "Send the API key again: it is never sent to an address it was not stored for.",
    )


def plex_pin_pending() -> ProblemError:
    """409: the owner-token PIN has not been approved on plex.tv yet."""
    return ProblemError(
        HTTPStatus.CONFLICT, "plex_pin_pending", "Approve the Plex PIN, then try again."
    )


def pin_expired() -> ProblemError:
    """410: an unknown, used or expired PIN handle, or one of another purpose or session."""
    return ProblemError(HTTPStatus.GONE, "pin_expired", "Start a new Plex PIN.")


def connector_failed(health: str) -> ProblemError:
    """502: the connection test failed; the health value becomes the problem code."""
    codes = {
        "unauthorized": "connector_unauthorized",
        "unreachable": "connector_unreachable",
        "unexpected_response": "connector_unexpected_response",
        "unsupported_version": "media_server_unsupported",
    }
    return ProblemError(
        HTTPStatus.BAD_GATEWAY,
        codes.get(health, "connector_unexpected_response"),
        "The media server did not answer as expected; nothing was saved.",
    )


def media_server_unsupported(detail: str) -> ProblemError:
    """502: the address answers, but not as the declared product or version."""
    return ProblemError(HTTPStatus.BAD_GATEWAY, "media_server_unsupported", detail)


def setup_required() -> ProblemError:
    """503: nobody can sign in until the console completes first-run setup."""
    return ProblemError(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "setup_required",
        "This server still has to be set up in its web console.",
    )
