"""ASGI middlewares: request ids with access logging, and security headers."""

import logging
import re
import time
import uuid
from collections.abc import Collection
from typing import Final

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tindeerr.core.logs import request_id_var

REQUEST_ID_HEADER: Final = "X-Request-ID"
_VALID_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9._-]{1,128}")
#: Successful requests to these paths (probes) are logged at debug level only.
_QUIET_PATHS: Final = frozenset({"/healthz"})
_FIRST_ERROR_STATUS: Final = 400

SECURITY_HEADERS: Final = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}
CONTENT_SECURITY_POLICY: Final = "default-src 'none'; frame-ancestors 'none'"

access_logger = logging.getLogger("tindeerr.access")


class RequestIdMiddleware:
    """Assigns a request id, echoes it in ``X-Request-ID`` and logs one line per request.

    A client-supplied id is kept when it is short and made of safe characters (so it can
    be correlated with a reverse proxy's logs); otherwise a new one is generated. The
    access log line has no query string, which could carry a credential.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI call."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = incoming if _VALID_REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        status = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path: str = scope["path"]
            client = scope.get("client")
            access_logger.log(
                logging.DEBUG
                if path in _QUIET_PATHS and status < _FIRST_ERROR_STATUS
                else logging.INFO,
                "request",
                extra={
                    "method": scope["method"],
                    "path": path,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "client": client[0] if client else None,
                },
            )
            request_id_var.reset(token)


class SecurityHeadersMiddleware:
    """Adds conservative security headers to every HTTP response.

    API responses are never cached and never framed. The strict Content-Security-Policy
    is skipped on ``csp_exempt_paths`` (the optional interactive docs, which load
    scripts).
    """

    def __init__(self, app: ASGIApp, csp_exempt_paths: Collection[str] = ()) -> None:
        self.app = app
        self.csp_exempt_paths = frozenset(csp_exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI call."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        apply_csp = scope["path"] not in self.csp_exempt_paths

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
                if apply_csp:
                    headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
            await send(message)

        await self.app(scope, receive, send_wrapper)
