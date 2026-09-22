"""The request context: client address, scheme, host and origin (docs/auth.md, §1).

Every request is resolved once, by ``RequestContextMiddleware``, into a small value that
everything else reads. Nothing downstream looks at ``X-Forwarded-*``, ``Host`` or
``Origin`` again, so there is a single place where a header becomes a decision.

- **Client address.** The TCP peer, unless it is in ``TINDARR_TRUSTED_PROXIES``: then
  it is the rightmost untrusted hop of ``X-Forwarded-For``, or the leftmost hop when
  every hop is trusted. A hop that is not an IP address means a misconfigured proxy: the
  peer is used and a warning is logged. Forwarded headers from an untrusted private peer
  also get one warning per peer and per hour ("add it to TINDARR_TRUSTED_PROXIES?"),
  without the header values.
- **Scheme.** The connection's, replaced by ``X-Forwarded-Proto`` only behind a trusted
  proxy (last value, ``http`` or ``https`` only). ``X-Forwarded-Host`` and
  ``X-Forwarded-Port`` are always ignored: the proxy must pass the original ``Host``.
- **Host.** An IP literal, ``localhost``, a name in ``TINDARR_ALLOWED_HOSTS`` or the
  host of ``public_url``; anything else gets ``400 host_not_allowed`` before routing
  (``AllowedHostMiddleware``), on every path but ``/healthz``.
- **Origin.** Built from the validated host, never from a header the client chose.
"""

import logging
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Final

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from tindarr.api.errors import problem_response
from tindarr.auth.errors import host_not_allowed
from tindarr.core.net import (
    LOCALHOST,
    HostPort,
    IPAddress,
    IPNetwork,
    Scheme,
    build_origin,
    in_networks,
    is_private_ip,
    parse_host,
    parse_ip,
    rate_limit_key,
)

#: Scope key holding the resolved ``RequestContext``.
CONTEXT_SCOPE_KEY: Final = "tindarr.context"
#: Scope key set to ``True`` when the direct peer is a trusted proxy.
TRUSTED_PEER_SCOPE_KEY: Final = "tindarr.trusted_peer"
#: Paths answered before the allowed-host check (the container's probe).
HOST_CHECK_EXEMPT_PATHS: Final = frozenset({"/healthz"})
#: How often the same peer is warned about forwarded headers it may not send.
WARNING_INTERVAL_S: Final = 3600.0
#: How many peers the warning throttle remembers before starting over.
_MAX_WARNED_PEERS: Final = 1024

_FORWARDED_SCHEMES: Final = {"http": ("http", "ws"), "https": ("https", "wss")}
#: Headers only a trusted proxy may set.
FORWARDED_HEADERS: Final = ("x-forwarded-for", "x-forwarded-proto", "forwarded", "x-request-id")

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RequestContext:
    """What the network says about one request, resolved once."""

    peer: IPAddress | None
    client: IPAddress | None
    scheme: Scheme
    host: HostPort | None
    host_allowed: bool
    trusted_peer: bool

    @property
    def client_is_private(self) -> bool:
        """Whether the resolved client address is on the local network."""
        return is_private_ip(self.client)

    @property
    def rate_limit_key(self) -> str:
        """The bucket rate limits count this client in."""
        return rate_limit_key(self.client)

    @property
    def origin(self) -> str | None:
        """The server's own origin for this request, or ``None`` without a valid host."""
        if self.host is None or not self.host_allowed:
            return None
        return build_origin(self.scheme, self.host)

    @property
    def is_loopback_to_localhost(self) -> bool:
        """A browser's secure context without TLS: loopback client and a localhost host.

        Browsers treat ``localhost``, ``127.0.0.1`` and ``[::1]`` as secure origins, so
        ``__Host-`` cookies work there over plain HTTP.
        """
        if self.host is None or self.client is None or not self.client.is_loopback:
            return False
        return self.host.is_localhost or (self.host.ip is not None and self.host.ip.is_loopback)


class HostPolicy:
    """The hosts this server answers to.

    ``TINDARR_ALLOWED_HOSTS`` is fixed at startup; the host of ``public_url`` is added
    when it is read from the settings and whenever it changes. Nothing else is ever
    accepted, not even for the length of a ``public_url`` check: the check's proof is
    bound to the host the request arrives at (docs/auth.md, section 10), so a candidate
    host this server does not answer to could not be verified anyway, and the console
    says so instead of opening the door for a few seconds.
    """

    def __init__(self, allowed_hosts: Collection[str] = ()) -> None:
        self._names = {name.lower() for name in allowed_hosts}
        self._public_url_host: str | None = None

    @property
    def public_url_host(self) -> str | None:
        """The host of ``public_url``, once it is set."""
        return self._public_url_host

    def set_public_url(self, public_url: str | None) -> None:
        """Accept (or stop accepting) the host of ``public_url`` as a ``Host``."""
        self._public_url_host = host_of(public_url)

    def allows_name(self, name: str) -> bool:
        """Whether a bare host name is one of ours (used before a ``public_url`` check)."""
        return self.allows(parse_host(name))

    def allows(self, host: HostPort | None) -> bool:
        """Whether a parsed ``Host`` header is one of ours (the port never matters)."""
        if host is None:
            return False
        if host.ip is not None or host.host == LOCALHOST:
            return True
        return host.host in self._names or host.host == self._public_url_host


def host_of(url: str | None) -> str | None:
    """Return the host of an origin, lower-cased and without its port, or ``None``."""
    if url is None:
        return None
    _, _, authority = url.partition("://")
    host = parse_host(authority)
    return None if host is None else host.host


def client_ip(scope: Scope) -> IPAddress | None:
    """Return the request's client address, or ``None`` when there is no usable one.

    Behind a trusted proxy this is the forwarded client address (see the middleware).
    """
    client = scope.get("client")
    if not client:
        return None
    return parse_ip(str(client[0]))


def request_context(scope: Scope) -> RequestContext:
    """Return the context resolved by the middleware for this request."""
    context = scope.get(CONTEXT_SCOPE_KEY)
    if not isinstance(context, RequestContext):  # pragma: no cover - wiring error
        msg = "the request context middleware did not run"
        raise TypeError(msg)
    return context


def forwarded_client(
    peer: IPAddress, forwarded_for: str, trusted: Collection[IPNetwork]
) -> IPAddress | None:
    """Return the client address behind a trusted proxy, or ``None`` for a bad chain.

    The rightmost hop outside ``trusted`` wins; if every hop is trusted, the leftmost
    one. ``None`` means an entry that is not an IP address, which the caller reports and
    falls back from to the peer itself.
    """
    client = peer
    for entry in reversed(forwarded_for.split(",")):
        if not entry.strip():
            continue
        hop = parse_ip(entry)
        if hop is None:
            return None
        client = hop
        if not in_networks(hop, tuple(trusted)):
            break
    return client


class RequestContextMiddleware:
    """Resolves the client address, the scheme, the host and the origin, once.

    Installed outermost, so everything inside (access log, rate limits, routes) sees the
    real client. It also normalises the peer address (IPv4-mapped to IPv4) and marks
    requests that came through a trusted proxy (``TRUSTED_PEER_SCOPE_KEY``).
    """

    def __init__(
        self,
        app: ASGIApp,
        trusted_proxies: Collection[IPNetwork] = (),
        hosts: HostPolicy | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.trusted_proxies = tuple(trusted_proxies)
        self.hosts = hosts or HostPolicy()
        self._monotonic = monotonic
        self._warned: dict[str, float] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI call."""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        scope = dict(scope)
        headers = Headers(scope=scope)
        peer = client_ip(scope)
        trusted = peer is not None and in_networks(peer, self.trusted_proxies)
        client = peer
        if peer is not None and trusted:
            client = self._forwarded_address(peer, headers)
            scheme = self._forwarded_scheme(headers, websocket=scope["type"] == "websocket")
            if scheme is not None:
                scope["scheme"] = scheme
        elif peer is not None:
            self._warn_about_forwarded_headers(peer, headers)
        host = parse_host(headers.get("host", ""))
        scope[TRUSTED_PEER_SCOPE_KEY] = trusted
        if peer is not None:
            scope["client"] = (str(client), scope["client"][1] if client == peer else 0)
        scope[CONTEXT_SCOPE_KEY] = RequestContext(
            peer=peer,
            client=client,
            scheme="https" if scope["scheme"] in ("https", "wss") else "http",
            host=host,
            host_allowed=self.hosts.allows(host),
            trusted_peer=trusted,
        )
        await self.app(scope, receive, send)

    def _forwarded_address(self, peer: IPAddress, headers: Headers) -> IPAddress:
        forwarded_for = ",".join(headers.getlist("x-forwarded-for"))
        if not forwarded_for:
            return peer
        client = forwarded_client(peer, forwarded_for, self.trusted_proxies)
        if client is None:
            self._warn(
                str(peer),
                "X-Forwarded-For from a trusted proxy holds an entry that is not an IP "
                "address; using the proxy's own address. Check the proxy configuration",
            )
            return peer
        return client

    def _warn_about_forwarded_headers(self, peer: IPAddress, headers: Headers) -> None:
        if not is_private_ip(peer):
            # A public peer sending these is normal noise from the Internet.
            return
        if not any(name in headers for name in FORWARDED_HEADERS):
            return
        self._warn(
            str(peer),
            "forwarded headers from an untrusted peer: they are ignored. If this is "
            "your reverse proxy, add it to TINDARR_TRUSTED_PROXIES",
        )

    def _warn(self, peer: str, message: str) -> None:
        now = self._monotonic()
        last = self._warned.get(peer)
        if last is not None and now - last < WARNING_INTERVAL_S:
            return
        self._warned[peer] = now
        if len(self._warned) > _MAX_WARNED_PEERS:
            self._warned.clear()
        logger.warning(message, extra={"peer": peer})

    @staticmethod
    def _forwarded_scheme(headers: Headers, *, websocket: bool) -> str | None:
        # Repeated headers are one chain, like X-Forwarded-For: the last value is the
        # one the nearest proxy appended, and it wins.
        chain = ",".join(headers.getlist("x-forwarded-proto"))
        value = chain.rsplit(",", 1)[-1].strip().lower()
        schemes = _FORWARDED_SCHEMES.get(value)
        if schemes is None:
            return None
        return schemes[1] if websocket else schemes[0]


class AllowedHostMiddleware:
    """Refuses a request whose ``Host`` is not one of ours, before routing.

    Installed inside the security headers and the request id, so the problem response
    still carries them. ``/healthz`` is exempt: the container's probe uses whatever
    address it was given.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI call."""
        if scope["type"] != "http" or scope["path"] in HOST_CHECK_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        context = request_context(scope)
        if context.host_allowed:
            await self.app(scope, receive, send)
            return
        problem = host_not_allowed()
        logger.warning(
            "refused a request for an unknown host name",
            extra={"peer": str(context.peer), "host_parsed": context.host is not None},
        )
        response = problem_response(problem.status, problem.code, detail=problem.detail)
        await response(scope, receive, send)
