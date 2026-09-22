"""Errors with a stable problem ``code``, raised by any layer and rendered by the API.

Auth, storage or the domain raise a ``ProblemError`` (or a subclass) without knowing
about HTTP; ``tindeerr.api.errors`` turns it into an RFC 9457 problem response with the
same status, ``code``, ``detail`` and headers. Clients switch on ``code``.

``detail`` is sent to the client as is: it must never contain a secret, a stack trace or
a value the user typed. For a 5xx status, the detail is only logged.
"""

from collections.abc import Mapping
from http import HTTPStatus
from types import MappingProxyType

_FIRST_ERROR_STATUS = HTTPStatus.BAD_REQUEST
_LAST_ERROR_STATUS = 599


class ProblemError(Exception):
    """An error the API reports as a problem with a stable ``code``."""

    def __init__(
        self,
        status: int,
        code: str,
        detail: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if not _FIRST_ERROR_STATUS <= status <= _LAST_ERROR_STATUS:
            msg = f"a problem needs an error status (4xx or 5xx), not {status}"
            raise ValueError(msg)
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers: Mapping[str, str] = MappingProxyType(dict(headers or {}))
