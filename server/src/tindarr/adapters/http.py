"""The HTTP client every adapter talks through, and how a remote failure is reported.

One place decides the things that must not differ between adapters: timeouts, no
redirect following, TLS verification, a bounded response body, and the fact that a
transport error or an unreadable body becomes a ``ProblemError`` with the caller's own
code rather than leaking an exception from the vendor's library.

- **Redirects are never followed.** A media server that answers `302` to another host
  would otherwise receive the API key or the user's password.
- **Bodies are bounded.** ``read_json`` and ``read_xml`` refuse anything larger than
  ``MAX_RESPONSE_BYTES``, so a hostile or broken server cannot make the process grow.
- **Nothing here logs a URL.** Quick Connect's secret travels in a query string
  (docs/auth.md, section 13), so the httpx loggers are kept at ``WARNING``
  (``tindarr.core.logs.quiet_noisy_libraries``) and adapters log their own lines.
"""

import json
import ssl
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Final, Self, cast

# ``read_xml`` explains why the standard parser is enough for the two plex.tv endpoints.
from xml.etree.ElementTree import Element, ParseError, fromstring

import httpx2

#: Connect, read and write timeout of every outbound call, in seconds.
DEFAULT_TIMEOUT_S: Final = 10.0
#: Reasons a call produced no response at all. ``tls_error`` is separated out because
#: ``public_url`` reports it to the administrator, who can act on it.
NO_RESPONSE_REASONS: Final = ("timeout", "unreachable", "tls_error")
#: Largest response body an adapter reads. Far above any real answer.
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
_XML_PARSE_ERROR: Final = "the response is not the XML this endpoint returns"


class RemoteCallError(Exception):
    """An outbound call failed before it produced a usable response.

    Carries no response body and no URL: the adapter turns it into the problem its port
    documents (``media_server_unreachable``, ``plex_tv_unreachable``…).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HttpSession:
    """A short-lived HTTP client with the adapters' defaults.

    Used as an async context manager around the calls of one operation, so no connection
    outlives the request that needed it and no state is shared between adapters.
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        headers: Mapping[str, str] | None = None,
        verify_tls: bool = True,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx2.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=dict(headers or {}),
            verify=verify_tls,
            timeout=timeout_s,
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        """Enter the context; the client is already built."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the client and its connections."""
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: object | None = None,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        """Make one call and return the response, whatever its status.

        Raises ``RemoteCallError`` when no response came back at all (DNS, TLS, timeout,
        a connection reset). The reason is a short word for the logs, never the URL.
        """
        try:
            return await self._client.request(
                method, url, json=json_body, params=dict(params or {}), headers=dict(headers or {})
            )
        except httpx2.TimeoutException:
            raise RemoteCallError("timeout") from None
        except httpx2.RequestError as failure:
            raise RemoteCallError("tls_error" if is_tls_error(failure) else "unreachable") from None

    async def get_bounded(self, url: str, limit: int = MAX_RESPONSE_BYTES) -> httpx2.Response:
        """GET a response, reading at most ``limit`` bytes of its body.

        ``request`` lets the client read the whole body before anything can look at it,
        which is fine for a media server the operator configured and wrong for an
        address somebody typed: this streams instead and gives up as soon as the body
        is longer than it could legitimately be.
        """
        try:
            async with self._client.stream("GET", url) as response:
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise RemoteCallError("oversized_response")
                    chunks.append(chunk)
        except httpx2.TimeoutException:
            raise RemoteCallError("timeout") from None
        except httpx2.RequestError as failure:
            raise RemoteCallError("tls_error" if is_tls_error(failure) else "unreachable") from None
        return httpx2.Response(
            response.status_code, headers=response.headers, content=b"".join(chunks)
        )


def is_tls_error(failure: BaseException) -> bool:
    """Whether a transport failure was the TLS handshake (a certificate, usually).

    ``httpx`` reports it as a ``ConnectError`` wrapping an ``ssl.SSLError``, so the
    cause chain is walked rather than the message parsed.
    """
    seen: BaseException | None = failure
    while seen is not None:
        if isinstance(seen, ssl.SSLError):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def body_bytes(response: httpx2.Response) -> bytes:
    """Return the response body, refusing one that is too large to be genuine."""
    content = response.content
    if len(content) > MAX_RESPONSE_BYTES:
        raise RemoteCallError("oversized_response")
    return content


def read_json(response: httpx2.Response) -> Any:
    """Return the response's JSON body.

    Raises ``RemoteCallError`` for a body that is not JSON or is too large, so every
    adapter reports "the server answered something unexpected" the same way.
    """
    try:
        return json.loads(body_bytes(response))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RemoteCallError("unexpected_response") from None


def as_object(value: object) -> Mapping[str, Any] | None:
    """Return a JSON value as an object, or ``None`` when it is anything else.

    Decoded JSON is ``Any``; this is where it becomes a type the rest of the adapter can
    be checked against, so no ``Any`` spreads further.
    """
    return cast("Mapping[str, Any]", value) if isinstance(value, dict) else None


def read_mapping(response: httpx2.Response) -> Mapping[str, Any]:
    """Return the response's JSON body when it is an object, else fail."""
    payload = as_object(read_json(response))
    if payload is None:
        raise RemoteCallError("unexpected_response")
    return payload


def read_list(response: httpx2.Response) -> list[Any]:
    """Return the response's JSON body when it is an array, else fail."""
    payload = read_json(response)
    if not isinstance(payload, list):
        raise RemoteCallError("unexpected_response")
    return cast("list[Any]", payload)


def read_xml(response: httpx2.Response) -> Element:
    """Return the root element of an XML response (plex.tv's older endpoints).

    ``xml.etree`` is used rather than a hardened parser because the two XML endpoints
    Tindarr calls are plex.tv's own, over TLS: it resolves no external entity and no
    DTD, and the body is capped by ``body_bytes`` first, which leaves nothing an
    entity-expansion attack could grow.
    """
    try:
        return fromstring(body_bytes(response))  # noqa: S314 - plex.tv over TLS, capped above
    except ParseError:
        raise RemoteCallError(_XML_PARSE_ERROR) from None


def as_text(value: object) -> str | None:
    """Return a JSON value as text when it is a non-empty string or a number."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return str(value)
    return None


def as_flag(value: object, *, default: bool) -> bool:
    """Return a JSON value as a boolean, falling back to ``default`` when it is absent.

    A media server that stops sending a policy field must not silently grant something:
    each caller passes the safe default for that field.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "1", "false", "0"):
        return value.lower() in ("true", "1")
    return default
