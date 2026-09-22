"""Addresses, hosts and origins (docs/auth.md, section 1)."""

from ipaddress import IPv4Address, IPv6Address

import pytest

from tindeerr.core.net import (
    build_origin,
    is_http_allowed_host,
    is_private_ip,
    normalize_origin,
    normalize_public_url,
    parse_host,
    parse_ip,
    rate_limit_key,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("203.0.113.7", IPv4Address("203.0.113.7")),
        (" 203.0.113.7 ", IPv4Address("203.0.113.7")),
        ("2001:db8::1", IPv6Address("2001:db8::1")),
        ("::ffff:10.0.0.2", IPv4Address("10.0.0.2")),
        ("::FFFF:172.18.0.1", IPv4Address("172.18.0.1")),
    ],
)
def test_parse_ip(value: str, expected: object) -> None:
    assert parse_ip(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "unknown", "testclient", "proxy.lan", "10.0.0.1:8080", "10.0.0.0/8", "1.2.3", "::g"],
)
def test_parse_ip_rejects_garbage(value: str) -> None:
    assert parse_ip(value) is None


@pytest.mark.parametrize(
    "value",
    ["127.0.0.1", "127.9.9.9", "10.1.2.3", "172.16.0.1", "192.168.1.4", "169.254.1.1", "::1"],
)
def test_private_addresses(value: str) -> None:
    assert is_private_ip(parse_ip(value))


@pytest.mark.parametrize(
    "value",
    # Carrier-grade NAT (Tailscale included) and public addresses are not private.
    ["100.64.0.1", "100.100.100.100", "8.8.8.8", "203.0.113.7", "2001:db8::1", "192.0.2.1"],
)
def test_public_addresses(value: str) -> None:
    assert not is_private_ip(parse_ip(value))


def test_unknown_address_is_not_private() -> None:
    assert not is_private_ip(None)


def test_rate_limit_key_groups_ipv6_by_64() -> None:
    assert rate_limit_key(IPv4Address("203.0.113.7")) == "203.0.113.7"
    first = rate_limit_key(IPv6Address("2001:db8:1:2:aaaa::1"))
    assert first == "2001:db8:1:2::/64"
    assert rate_limit_key(IPv6Address("2001:db8:1:2:ffff:ffff:ffff:ffff")) == first
    assert rate_limit_key(IPv6Address("2001:db8:1:3::1")) != first
    assert rate_limit_key(None) == "unknown"


@pytest.mark.parametrize(
    ("value", "host", "port"),
    [
        ("tindeerr.example.com", "tindeerr.example.com", None),
        ("Tindeerr.Example.COM:8787", "tindeerr.example.com", 8787),
        ("example.com.", "example.com", None),
        ("localhost", "localhost", None),
        ("192.168.1.4:8787", "192.168.1.4", 8787),
        ("[::1]", "::1", None),
        ("[2001:db8::1]:8787", "2001:db8::1", 8787),
        ("xn--tindeerr-1ya.example", "xn--tindeerr-1ya.example", None),
    ],
)
def test_parse_host(value: str, host: str, port: int | None) -> None:
    parsed = parse_host(value)
    assert parsed is not None
    assert (parsed.host, parsed.port) == (host, port)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "::1",  # IPv6 must be bracketed
        "example.com:0",
        "example.com:99999",
        "example.com:",
        "example.com:https",
        "user@example.com",
        "example.com/path",
        "exa mple.com",
        "-example.com",
        "[not-an-ip]",
        "[::1",
        "[::1]x",
        "a" * 64 + ".example.com",
    ],
)
def test_parse_host_rejects(value: str) -> None:
    assert parse_host(value) is None


@pytest.mark.parametrize(
    ("scheme", "value", "expected"),
    [
        ("https", "example.com", "https://example.com"),
        ("https", "example.com:443", "https://example.com"),
        ("https", "example.com:8443", "https://example.com:8443"),
        ("http", "example.com:80", "http://example.com"),
        ("http", "[::1]:8787", "http://[::1]:8787"),
        ("https", "[2001:DB8::1]", "https://[2001:db8::1]"),
    ],
)
def test_build_origin(scheme: str, value: str, expected: str) -> None:
    host = parse_host(value)
    assert host is not None
    assert build_origin("https" if scheme == "https" else "http", host) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://Example.com", "https://example.com"),
        ("https://example.com:443", "https://example.com"),
        ("HTTP://192.168.1.4:8787", "http://192.168.1.4:8787"),
        ("  https://example.com  ", "https://example.com"),
    ],
)
def test_normalize_origin(value: str, expected: str) -> None:
    assert normalize_origin(value) == expected


@pytest.mark.parametrize(
    "value",
    ["null", "", "example.com", "file://x", "https://example.com/path", "https://a@example.com"],
)
def test_normalize_origin_refuses(value: str) -> None:
    assert normalize_origin(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://tindeerr.example.com", "https://tindeerr.example.com"),
        ("https://tindeerr.example.com/", "https://tindeerr.example.com"),
        ("https://tindeerr.example.com:8443", "https://tindeerr.example.com:8443"),
        ("http://192.168.1.4:8787", "http://192.168.1.4:8787"),
        ("http://localhost:8787", "http://localhost:8787"),
        ("http://tindeerr.local", "http://tindeerr.local"),
        ("http://[fd00::1]", "http://[fd00::1]"),
    ],
)
def test_public_url(value: str, expected: str) -> None:
    assert normalize_public_url(value) == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("tindeerr.example.com", "https"),
        ("ftp://example.com", "https"),
        ("http://example.com", "https"),
        ("http://8.8.8.8", "https"),
        # Carrier-grade NAT is not a private address.
        ("http://100.64.0.1", "https"),
        ("https://example.com/console", "origin"),
        ("https://user@example.com", "origin"),
        ("https://example.com?a=1", "origin"),
        ("https://example.com#x", "origin"),
        ("https://exa mple.com", "invalid host"),
        ("https://example.com:0", "invalid host"),
    ],
)
def test_public_url_refuses(value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_public_url(value)


def test_http_hosts_are_decided_without_dns() -> None:
    for value in ("192.168.1.4", "localhost", "tindeerr.local", "[::1]"):
        host = parse_host(value)
        assert host is not None
        assert is_http_allowed_host(host)
    for value in ("example.com", "8.8.8.8", "localhost.example.com"):
        host = parse_host(value)
        assert host is not None
        assert not is_http_allowed_host(host)
