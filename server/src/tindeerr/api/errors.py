"""RFC 9457 problem details for every error the API returns.

Clients switch on ``code``, never on ``title`` or ``detail``. Exception messages and
tracebacks are logged (redacted), never sent to the client.

- ``ProblemError`` (``tindeerr.core.errors``), raised by any layer, keeps its status,
  code, detail, headers and extensions; for a 5xx its detail is logged, not sent.
- ``DecryptionError`` (a stored secret that no longer decrypts, usually after a change of
  secret key) is logged with its own message and answered as a generic 500.
- Framework errors (404, 405, 429...) get a code from their status; invalid input is a
  400 ``validation_error`` listing fields and messages, never the rejected value.
- Anything else is a generic 500 ``internal_error``.
"""

import logging
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Final, TypedDict

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tindeerr.core.crypto import DecryptionError
from tindeerr.core.errors import ProblemError

PROBLEM_MEDIA_TYPE: Final = "application/problem+json"

_CODES_BY_STATUS: Final[Mapping[int, str]] = {
    HTTPStatus.BAD_REQUEST: "validation_error",
    HTTPStatus.UNAUTHORIZED: "unauthorized",
    HTTPStatus.FORBIDDEN: "forbidden",
    HTTPStatus.NOT_FOUND: "not_found",
    HTTPStatus.METHOD_NOT_ALLOWED: "method_not_allowed",
    HTTPStatus.TOO_MANY_REQUESTS: "rate_limited",
}

logger = logging.getLogger(__name__)


class FieldError(TypedDict):
    """One entry of ``errors`` in a ``validation_error`` problem."""

    field: str
    message: str


def problem_response(  # noqa: PLR0913 - one parameter per documented problem member
    status: int,
    code: str,
    *,
    detail: str | None = None,
    errors: Sequence[FieldError] | None = None,
    headers: Mapping[str, str] | None = None,
    extensions: Mapping[str, str | int] | None = None,
) -> JSONResponse:
    """Build an ``application/problem+json`` response."""
    body: dict[str, object] = dict(extensions or {})
    body |= {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "code": code,
    }
    if detail is not None:
        body["detail"] = detail
    if errors is not None:
        body["errors"] = list(errors)
    return JSONResponse(
        jsonable_encoder(body),
        status_code=status,
        headers=dict(headers) if headers else None,
        media_type=PROBLEM_MEDIA_TYPE,
    )


def _code_for_status(status: int) -> str:
    if status in _CODES_BY_STATUS:
        return _CODES_BY_STATUS[status]
    return "bad_request" if status < HTTPStatus.INTERNAL_SERVER_ERROR else "internal_error"


async def _http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101 - registered for this type
    return problem_response(exc.status_code, _code_for_status(exc.status_code), headers=exc.headers)


async def _validation_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered for this type
    # Only the location and pydantic's message: the rejected input may be a secret.
    errors = [
        FieldError(
            field=".".join(str(part) for part in error.get("loc", ())),
            message=str(error.get("msg", "invalid value")),
        )
        for error in exc.errors()
    ]
    return problem_response(HTTPStatus.BAD_REQUEST, "validation_error", errors=errors)


async def _problem_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)  # noqa: S101 - registered for this type
    if exc.status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        logger.error("request failed", exc_info=exc, extra={"problem": exc.code})
        return problem_response(
            exc.status, exc.code, headers=exc.headers, extensions=exc.extensions
        )
    return problem_response(
        exc.status, exc.code, detail=exc.detail, headers=exc.headers, extensions=exc.extensions
    )


async def _decryption_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "a stored secret cannot be decrypted; was the secret key changed?",
        exc_info=exc,
        extra={"problem": "decryption_failed"},
    )
    return problem_response(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error")


def install_error_handlers(app: FastAPI) -> None:
    """Render domain errors, framework errors and invalid input as problem details."""
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(ProblemError, _problem_error_handler)
    app.add_exception_handler(DecryptionError, _decryption_error_handler)


class UnhandledErrorMiddleware:
    """Turns any unhandled exception into a generic ``internal_error`` problem.

    Installed inside the other middlewares, so the 500 response still gets the request
    id and security headers. When the response has already started, a problem can no
    longer be sent: the exception is re-raised so the server aborts the connection
    instead of leaving the client with a truncated body that looks complete.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the app; answer 500 if it raises before starting a response."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("unhandled error")
            if response_started:
                raise
            response = problem_response(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error")
            await response(scope, receive, send)
