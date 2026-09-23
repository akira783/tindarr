"""The problems an adapter reports, with the contract's ``code`` values.

An adapter knows nothing about ``tindarr.auth``, but a failed sign-in must answer the
same way wherever it was detected. These constructors are that shared vocabulary: the
adapters raise them, ``tindarr.auth`` re-exports the ones it also raises itself
(``tindarr.auth.errors``), and the API layer only renders them.

Details never name a user, a password or a token, and never repeat a remote response
body (the security model, section 7): the caller learns what to do, not what the media
server said.
"""

from http import HTTPStatus

from tindarr.core.errors import ProblemError

__all__ = [
    "account_disabled",
    "invalid_credentials",
    "llm_auth_failed",
    "llm_invalid_output",
    "llm_model_not_found",
    "llm_quota",
    "llm_unreachable",
    "media_server_changed",
    "media_server_unreachable",
    "media_server_unsupported",
    "metadata_unreachable",
    "not_a_server_user",
    "plex_owner_required",
    "plex_tv_unreachable",
    "quick_connect_expired",
    "quick_connect_unavailable",
    "quota_exceeded",
    "request_backend_error",
    "request_not_allowed",
    "sign_in_method_unavailable",
]


def invalid_credentials() -> ProblemError:
    """401: wrong password, unknown user, or an account that is not the session's user.

    One answer for all of them: the caller must not learn which accounts exist.
    """
    return ProblemError(
        HTTPStatus.UNAUTHORIZED, "invalid_credentials", "Wrong user name or password."
    )


def account_disabled() -> ProblemError:
    """403: the account is disabled, here or on the media server."""
    return ProblemError(HTTPStatus.FORBIDDEN, "account_disabled", "This account is disabled.")


def not_a_server_user() -> ProblemError:
    """403: no plex.tv resource of that account matches the configured server."""
    return ProblemError(
        HTTPStatus.FORBIDDEN,
        "not_a_server_user",
        "This Plex account has no access to the configured server.",
    )


def plex_owner_required() -> ProblemError:
    """403: the owner token must come from the account that owns the Plex server."""
    return ProblemError(
        HTTPStatus.FORBIDDEN,
        "plex_owner_required",
        "Sign in with the Plex account that owns this server.",
    )


def sign_in_method_unavailable(detail: str) -> ProblemError:
    """409: the configured media server does not offer this way of signing in."""
    return ProblemError(HTTPStatus.CONFLICT, "sign_in_method_unavailable", detail)


def quick_connect_unavailable() -> ProblemError:
    """409: not a Jellyfin server, or Quick Connect is switched off there."""
    return ProblemError(
        HTTPStatus.CONFLICT,
        "quick_connect_unavailable",
        "Quick Connect is not available on this media server.",
    )


def quick_connect_expired() -> ProblemError:
    """410: an unknown, used or expired Quick Connect handle, or one bound elsewhere."""
    return ProblemError(
        HTTPStatus.GONE, "quick_connect_expired", "Start a new Quick Connect request."
    )


def media_server_unsupported(detail: str) -> ProblemError:
    """502: the address answers, but not as the declared product or a supported version."""
    return ProblemError(HTTPStatus.BAD_GATEWAY, "media_server_unsupported", detail)


def media_server_unreachable() -> ProblemError:
    """503: the media server did not answer, or answered something unusable."""
    return ProblemError(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "media_server_unreachable",
        "The media server did not answer; try again in a moment.",
    )


def media_server_changed() -> ProblemError:
    """503: something else answers at the configured address (docs/auth.md, section 6)."""
    return ProblemError(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "media_server_changed",
        "This is not the media server Tindarr was set up with; an administrator must "
        "check the connector.",
    )


def plex_tv_unreachable() -> ProblemError:
    """503: plex.tv did not answer, or answered something unusable."""
    return ProblemError(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "plex_tv_unreachable",
        "plex.tv did not answer; try again in a moment.",
    )


# --- metadata, the request backend and the AI providers (step 3) --------------------


def metadata_unreachable() -> ProblemError:
    """503: TMDb or OMDb did not answer, or answered something unusable."""
    return ProblemError(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "metadata_unreachable",
        "The metadata service did not answer; try again in a moment.",
    )


def request_backend_error() -> ProblemError:
    """502: the request backend answered something Tindarr cannot act on."""
    return ProblemError(
        HTTPStatus.BAD_GATEWAY,
        "request_backend_error",
        "The request service answered with an error; an administrator should check it.",
    )


def request_not_allowed() -> ProblemError:
    """403: the backend refuses this request for this user (permissions)."""
    return ProblemError(
        HTTPStatus.FORBIDDEN,
        "request_not_allowed",
        "Your account on the request service may not request this.",
    )


def quota_exceeded() -> ProblemError:
    """403: the backend's own per-user quota is spent."""
    return ProblemError(
        HTTPStatus.FORBIDDEN,
        "quota_exceeded",
        "Your request quota on the request service is used up for now.",
    )


def llm_auth_failed() -> ProblemError:
    """502: the AI provider rejected the API key."""
    return ProblemError(
        HTTPStatus.BAD_GATEWAY,
        "llm_auth_failed",
        "The AI provider rejected the API key; an administrator should check it.",
    )


def llm_quota() -> ProblemError:
    """502: the AI provider's rate limit or paid quota is exhausted."""
    return ProblemError(
        HTTPStatus.BAD_GATEWAY,
        "llm_quota",
        "The AI provider's rate limit or quota was reached; try again later.",
    )


def llm_model_not_found() -> ProblemError:
    """502: the AI provider does not serve the configured model."""
    return ProblemError(
        HTTPStatus.BAD_GATEWAY,
        "llm_model_not_found",
        "The AI provider does not know the configured model.",
    )


def llm_unreachable() -> ProblemError:
    """502: the AI provider did not answer at all."""
    return ProblemError(
        HTTPStatus.BAD_GATEWAY,
        "llm_unreachable",
        "The AI provider could not be reached; check its address and the network.",
    )


def llm_invalid_output() -> ProblemError:
    """502: the model's answer did not fit the schema, twice running."""
    return ProblemError(
        HTTPStatus.BAD_GATEWAY,
        "llm_invalid_output",
        "The AI provider did not answer in the expected format; try again.",
    )
