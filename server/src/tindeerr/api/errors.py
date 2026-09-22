"""RFC 9457 problem details for every error the API returns.

Clients switch on ``code``, never on ``title`` or ``detail``. Exception messages and
tracebacks are logged (redacted), never sent to the client.
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

PROBLEM_MEDIA_TYPE: Final = "application/problem+json"

_CODES_BY_STATUS: Final[Mapping[int, str]] = {
    HTTPStatus.BAD_REQUEST: "validation_error",
    HTTPStatus.NOT_FOUND: "not_found",
    HTTPStatus.METHOD_NOT_ALLOWED: "method_not_allowed",
}

logger = logging.getLogger(__name__)


class FieldError(TypedDict):
    """One entry of ``errors`` in a ``validation_error`` problem."""

    field: str
    message: str


def problem_response(
    status: int,
    code: str,
    *,
    detail: str | None = None,
    errors: Sequence[FieldError] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build an ``application/problem+json`` response."""
    body: dict[str, object] = {
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


def install_error_handlers(app: FastAPI) -> None:
    """Render framework errors (404, 405, invalid input) as problem details."""
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)


class UnhandledErrorMiddleware:
    """Turns any unhandled exception into a generic ``internal_error`` problem.

    Installed inside the other middlewares, so the 500 response still gets the request
    id and security headers.
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
            if not response_started:
                response = problem_response(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error")
                await response(scope, receive, send)
