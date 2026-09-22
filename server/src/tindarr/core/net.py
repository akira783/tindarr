"""Addresses, hosts and origins: parsing and the "private network" rule.

Pure functions on strings and ``ipaddress`` values, shared by the API layer (which reads
them from requests) and the auth layer (which decides with them). Nothing here looks up
DNS: every decision is made on the text and on IP ranges (docs/auth.md, section 1).
"""

import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network
from typing import Final, Literal

type IPAddress = IPv4Address | IPv6Address
type IPNetwork = IPv4Network | IPv6Network
type Scheme = Literal["http", "https"]

#: IPv6 clients are grouped by /64, the smallest prefix a home or VPS usually gets.
IPV6_RATE_LIMIT_PREFIX: Final = 64

#: Client addresses treated as the local network. Nothing else counts: in particular
#: carrier-grade NAT (100.64.0.0/10, also used by Tailscale) is not private.
PRIVATE_NETWORKS: Final[tuple[IPNetwork, ...]] = tuple(
    ip_network(network)
    for network in (
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
    )
)

_DEFAULT_PORTS: Final[dict[Scheme, int]] = {"http": 80, "https": 443}
_MAX_PORT: Final = 65535
_MAX_HOST_NAME_LENGTH: Final = 253
_HOST_NAME_LABEL: Final = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
_PORT: Final = re.compile(r"[0-9]{1,5}")
LOCALHOST: Final = "localhost"


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


def is_private_ip(address: IPAddress | None) -> bool:
    """Whether a resolved client address is on the local network (``PRIVATE_NETWORKS``).

    ``None`` (no usable address) is never private.
    """
    if address is None:
        return False
    return any(address in network for network in PRIVATE_NETWORKS)


def in_networks(address: IPAddress, networks: tuple[IPNetwork, ...]) -> bool:
    """Whether ``address`` belongs to one of ``networks``."""
    return any(address in network for network in networks)


def rate_limit_key(address: IPAddress | None) -> str:
    """Return the rate limiter bucket for ``address``: itself for IPv4, its /64 for IPv6.

    One IPv6 host usually owns a whole /64, so per-address buckets would let it rotate
    addresses freely. Requests without a usable address share one bucket.
    """
    if address is None:
        return "unknown"
    if isinstance(address, IPv4Address):
        return str(address)
    return str(ip_network(f"{address}/{IPV6_RATE_LIMIT_PREFIX}", strict=False))


@dataclass(frozen=True)
class HostPort:
    """A parsed ``Host`` header or URL authority.

    ``host`` is lower-cased, without a trailing dot, and, for an IP literal, in the
    canonical text form of ``ipaddress`` (IPv6 without brackets).
    """

    host: str
    port: int | None
    ip: IPAddress | None

    @property
    def is_localhost(self) -> bool:
        """``localhost`` itself (not a name under it)."""
        return self.host == LOCALHOST

    def netloc(self, scheme: Scheme) -> str:
        """``host[:port]`` as in an origin: IPv6 in brackets, default port left out."""
        host = f"[{self.host}]" if isinstance(self.ip, IPv6Address) else self.host
        if self.port is None or self.port == _DEFAULT_PORTS[scheme]:
            return host
        return f"{host}:{self.port}"


def _parse_port(text: str) -> int | None:
    if not _PORT.fullmatch(text):
        return None
    port = int(text)
    return port if 0 < port <= _MAX_PORT else None


def _is_host_name(name: str) -> bool:
    return len(name) <= _MAX_HOST_NAME_LENGTH and all(
        _HOST_NAME_LABEL.fullmatch(label) for label in name.split(".")
    )


def _parse_bracketed_ipv6(value: str) -> HostPort | None:
    end = value.find("]")
    if end == -1:
        return None
    literal, rest = value[1:end], value[end + 1 :]
    port: int | None = None
    if rest and (not rest.startswith(":") or (port := _parse_port(rest[1:])) is None):
        return None
    try:
        address = IPv6Address(literal)
    except ValueError:
        return None
    return HostPort(str(address), port, address)


def parse_host(value: str) -> HostPort | None:
    """Parse ``host[:port]``, ``a.b.c.d[:port]`` or ``[ipv6][:port]``; ``None`` if invalid.

    IPv6 must be in brackets; user info, paths and anything but a DNS-style name or an
    IP literal are refused.
    """
    if value.startswith("["):
        return _parse_bracketed_ipv6(value)
    port: int | None = None
    name, colon, port_text = value.partition(":")
    if colon and (port := _parse_port(port_text)) is None:
        return None
    name = name.lower().removesuffix(".")
    try:
        address4 = IPv4Address(name)
    except ValueError:
        if not name or not _is_host_name(name):
            return None
        return HostPort(name, port, None)
    return HostPort(str(address4), port, address4)


def build_origin(scheme: Scheme, host: HostPort) -> str:
    """``<scheme>://<host>[:<port>]``, normalised like a browser's ``Origin`` header."""
    return f"{scheme}://{host.netloc(scheme)}"


def normalize_origin(value: str) -> str | None:
    """Parse an ``Origin`` header value and return it normalised, or ``None``.

    ``null``, other schemes, paths and anything unparsable give ``None``, so they never
    equal the server's origin.
    """
    scheme, separator, authority = value.strip().partition("://")
    scheme = scheme.lower()
    if not separator or scheme not in _DEFAULT_PORTS:
        return None
    host = parse_host(authority)
    if host is None:
        return None
    return build_origin("https" if scheme == "https" else "http", host)


def is_http_allowed_host(host: HostPort) -> bool:
    """Hosts that may be reached over plain ``http`` in ``public_url`` and pairing links.

    An IP literal in a private range, ``localhost`` or a ``.local`` name (decided on the
    text, never through DNS).
    """
    if host.ip is not None:
        return is_private_ip(host.ip)
    return host.is_localhost or host.host.endswith(".local")


def normalize_public_url(value: str) -> str:
    """Validate a ``public_url`` and return it as a bare origin (no trailing slash).

    It must be ``https://host[:port]``, or ``http://`` for a host accepted by
    ``is_http_allowed_host``; no user info, path, query or fragment. Raises
    ``ValueError`` with a message that does not repeat the value.
    """
    scheme, separator, rest = value.strip().partition("://")
    scheme = scheme.lower()
    if not separator or scheme not in _DEFAULT_PORTS:
        msg = "must start with https:// (or http:// for a private address)"
        raise ValueError(msg)
    authority = rest.removesuffix("/")
    if any(character in authority for character in "/?#@\\"):
        msg = "must be an origin: no user info, path, query or fragment"
        raise ValueError(msg)
    host = parse_host(authority)
    if host is None:
        msg = "has an invalid host or port"
        raise ValueError(msg)
    if scheme == "http" and not is_http_allowed_host(host):
        msg = "must use https unless the host is a private IP address, localhost or .local"
        raise ValueError(msg)
    return build_origin("https" if scheme == "https" else "http", host)
