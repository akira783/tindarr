"""Errors with a stable problem ``code``, raised by any layer and rendered by the API.

Auth, storage or the domain raise a ``ProblemError`` (or a subclass) without knowing
about HTTP; ``tindeerr.api.errors`` turns it into an RFC 9457 problem response with the
same status, ``code``, ``detail`` and headers. Clients switch on ``code``.

``detail`` is sent to the client as is: it must never contain a secret, a stack trace or
a value the user typed. For a 5xx status, the detail is only logged. ``extensions`` are
extra members of the problem body that the contract documents (``retry_after_ms``,
``reason``).
"""

import math
from collections.abc import Mapping
from http import HTTPStatus
from types import MappingProxyType
from typing import Final

_FIRST_ERROR_STATUS = HTTPStatus.BAD_REQUEST
_LAST_ERROR_STATUS = 599
#: Members of the problem body an extension may not replace.
RESERVED_MEMBERS: Final = frozenset({"type", "title", "status", "code", "detail", "errors"})


class ProblemError(Exception):
    """An error the API reports as a problem with a stable ``code``."""

    def __init__(
        self,
        status: int,
        code: str,
        detail: str | None = None,
        headers: Mapping[str, str] | None = None,
        extensions: Mapping[str, str | int] | None = None,
    ) -> None:
        if not _FIRST_ERROR_STATUS <= status <= _LAST_ERROR_STATUS:
            msg = f"a problem needs an error status (4xx or 5xx), not {status}"
            raise ValueError(msg)
        if extensions and RESERVED_MEMBERS & set(extensions):
            msg = f"extensions cannot replace {sorted(RESERVED_MEMBERS & set(extensions))}"
            raise ValueError(msg)
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers: Mapping[str, str] = MappingProxyType(dict(headers or {}))
        self.extensions: Mapping[str, str | int] = MappingProxyType(dict(extensions or {}))


class RateLimitedError(ProblemError):
    """``429 rate_limited``, with ``retry_after_ms`` and ``Retry-After`` (seconds, rounded up)."""

    def __init__(self, retry_after_ms: int, detail: str | None = None) -> None:
        retry_after_ms = max(retry_after_ms, 0)
        super().__init__(
            HTTPStatus.TOO_MANY_REQUESTS,
            "rate_limited",
            detail or "Too many requests; try again later.",
            headers={"Retry-After": str(math.ceil(retry_after_ms / 1000))},
            extensions={"retry_after_ms": retry_after_ms},
        )
        self.retry_after_ms = retry_after_ms
