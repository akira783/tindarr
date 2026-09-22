"""Client addresses, and the ``X-Forwarded-*`` headers of trusted reverse proxies.

``client_ip`` is the one way to read a request's client address. ``rate_limit_key``
turns it into the bucket key of a rate limiter.

``TrustedProxyMiddleware`` replaces the peer address with the client address from
``X-Forwarded-For``, and the scheme with ``X-Forwarded-Proto``, only when the peer is a
trusted proxy (``TINDEERR_TRUSTED_PROXIES``). The chain is read from the right: every
hop appended by a trusted proxy is believed, and the first untrusted one is the client.
Entries further left were written by the client itself and are ignored. An entry that
is not an IP address stops the walk: the last trusted hop is then used, so a
malformed or spoofed header can degrade the result to the proxy's address but never
to an address of the client's choosing.
"""

import logging
from collections.abc import Collection
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network
from typing import Final

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

type IPAddress = IPv4Address | IPv6Address
type IPNetwork = IPv4Network | IPv6Network

#: Scope key set to ``True`` when the direct peer is a trusted proxy.
TRUSTED_PEER_SCOPE_KEY: Final = "tindeerr.trusted_peer"
#: IPv6 clients are grouped by /64, the smallest prefix a home or VPS usually gets.
IPV6_RATE_LIMIT_PREFIX: Final = 64
_FORWARDED_SCHEMES: Final = {"http": ("http", "ws"), "https": ("https", "wss")}

logger = logging.getLogger(__name__)


def parse_ip(value: str) -> IPAddress | None:
    """Parse an address; IPv4-mapped IPv6 (``::ffff:10.0.0.1``) becomes IPv4.

    Returns ``None`` for anything that is not a bare IP address (host names, ports,
    CIDR blocks, garbage).
    """
    try:
        address = ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def client_ip(scope: Scope) -> IPAddress | None:
    """Return the request's client address, or ``None`` when there is no usable one.

    Behind a trusted proxy this is the forwarded client address (see the middleware).
    """
    client = scope.get("client")
    if not client:
        return None
    return parse_ip(str(client[0]))


def rate_limit_key(address: IPAddress) -> str:
    """Return the rate limiter bucket for ``address``: itself for IPv4, its /64 for IPv6.

    One IPv6 host usually owns a whole /64, so per-address buckets would let it rotate
    addresses freely.
    """
    if isinstance(address, IPv4Address):
        return str(address)
    return str(ip_network(f"{address}/{IPV6_RATE_LIMIT_PREFIX}", strict=False))


def _is_trusted(address: IPAddress, trusted: Collection[IPNetwork]) -> bool:
    return any(address in network for network in trusted)


def forwarded_client(
    peer: IPAddress, forwarded_for: str, trusted: Collection[IPNetwork]
) -> IPAddress:
    """Return the client address given a trusted ``peer`` and its ``X-Forwarded-For``."""
    client = peer
    for entry in reversed(forwarded_for.split(",")):
        if not entry.strip():
            continue
        hop = parse_ip(entry)
        if hop is None:
            break
        client = hop
        if not _is_trusted(hop, trusted):
            break
    return client


class TrustedProxyMiddleware:
    """Honours ``X-Forwarded-For`` / ``-Proto`` from trusted proxies only.

    Installed outermost, so everything inside (access log, rate limits, routes) sees the
    real client. Also normalises the peer address (IPv4-mapped to IPv4), marks requests
    coming through a trusted proxy (``TRUSTED_PEER_SCOPE_KEY``), and warns once when a
    private but untrusted peer sends ``X-Forwarded-For``: most likely a reverse proxy
    missing from ``TINDEERR_TRUSTED_PROXIES``, which would put every client in the
    proxy's rate limit bucket.
    """

    def __init__(self, app: ASGIApp, trusted_proxies: Collection[IPNetwork] = ()) -> None:
        self.app = app
        self.trusted_proxies = tuple(trusted_proxies)
        self._warned = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI call."""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        peer = client_ip(scope)
        if peer is None:
            await self.app(scope, receive, send)
            return
        scope = dict(scope)
        headers = Headers(scope=scope)
        forwarded_for = ",".join(headers.getlist("x-forwarded-for"))
        client = peer
        trusted = _is_trusted(peer, self.trusted_proxies)
        if trusted:
            if forwarded_for:
                client = forwarded_client(peer, forwarded_for, self.trusted_proxies)
            scheme = self._forwarded_scheme(headers, websocket=scope["type"] == "websocket")
            if scheme is not None:
                scope["scheme"] = scheme
        elif forwarded_for and peer.is_private and not self._warned:
            self._warned = True
            logger.warning(
                "X-Forwarded-For received from a private address that is not a trusted "
                "proxy; the header is ignored. If this is your reverse proxy, add it to "
                "TINDEERR_TRUSTED_PROXIES",
                extra={"peer": str(peer)},
            )
        scope[TRUSTED_PEER_SCOPE_KEY] = trusted
        scope["client"] = (str(client), scope["client"][1] if client == peer else 0)
        await self.app(scope, receive, send)

    @staticmethod
    def _forwarded_scheme(headers: Headers, *, websocket: bool) -> str | None:
        value = headers.get("x-forwarded-proto", "").rsplit(",", 1)[-1].strip().lower()
        schemes = _FORWARDED_SCHEMES.get(value)
        if schemes is None:
            return None
        return schemes[1] if websocket else schemes[0]
