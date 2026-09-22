import logging
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from tindeerr.api.proxy import client_ip, forwarded_client, parse_ip, rate_limit_key
from tindeerr.core.config import ServerConfig
from tindeerr.main.app import create_app

TRUSTED = (ip_network("10.0.0.0/8"), ip_network("fd00::/8"))


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


def test_client_ip_reads_the_scope() -> None:
    assert client_ip({"client": ("::ffff:192.0.2.1", 1)}) == IPv4Address("192.0.2.1")
    assert client_ip({"client": ("not-an-ip", 1)}) is None
    assert client_ip({"client": None}) is None
    assert client_ip({}) is None


def test_rate_limit_key_groups_ipv6_by_64() -> None:
    assert rate_limit_key(IPv4Address("203.0.113.7")) == "203.0.113.7"
    first = rate_limit_key(IPv6Address("2001:db8:1:2:aaaa::1"))
    assert first == "2001:db8:1:2::/64"
    assert rate_limit_key(IPv6Address("2001:db8:1:2:ffff:ffff:ffff:ffff")) == first
    assert rate_limit_key(IPv6Address("2001:db8:1:3::1")) != first


@pytest.mark.parametrize(
    ("forwarded_for", "expected"),
    [
        ("203.0.113.7", "203.0.113.7"),
        # Leftmost entries come from the client: only the rightmost untrusted counts.
        ("198.51.100.1, 203.0.113.7", "203.0.113.7"),
        ("198.51.100.1, 203.0.113.7, 10.0.0.9", "203.0.113.7"),
        ("1.1.1.1,2.2.2.2 , 203.0.113.7", "203.0.113.7"),
        # Garbage stops the walk at the last trusted hop, never at a spoofed address.
        ("garbage", "10.0.0.2"),
        ("203.0.113.7, garbage", "10.0.0.2"),
        ("198.51.100.1, garbage, 10.0.0.9", "10.0.0.9"),
        ("203.0.113.7:4444", "10.0.0.2"),
        ("unknown", "10.0.0.2"),
        (", ,", "10.0.0.2"),
        # IPv6 and IPv4-mapped entries.
        ("2001:db8::1", "2001:db8::1"),
        ("2001:db8::1, fd00::5", "2001:db8::1"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("::ffff:10.0.0.3, 203.0.113.7", "203.0.113.7"),
        # Only trusted hops: the leftmost one is the best guess.
        ("10.0.0.7, 10.0.0.8", "10.0.0.7"),
    ],
)
def test_forwarded_client(forwarded_for: str, expected: str) -> None:
    peer = IPv4Address("10.0.0.2")
    assert forwarded_client(peer, forwarded_for, TRUSTED) == ip_address(expected)


def make_client(
    data_dir: Path, peer: str, trusted: str | None = "10.0.0.0/8,fd00::/8"
) -> TestClient:
    config = ServerConfig(data_dir=data_dir, trusted_proxies=trusted or ())  # pyright: ignore[reportArgumentType]
    app = create_app(config)
    add_whoami(app)
    return TestClient(app, client=(peer, 5000))


def add_whoami(app: FastAPI) -> None:
    async def whoami(request: Request) -> dict[str, str | None]:
        return {
            "client": request.client.host if request.client else None,
            "scheme": request.url.scheme,
        }

    app.add_api_route("/test/whoami", whoami)


@pytest.mark.parametrize(
    ("peer", "headers", "expected"),
    [
        ("10.0.0.2", {"X-Forwarded-For": "198.51.100.1, 203.0.113.7"}, "203.0.113.7"),
        ("::ffff:10.0.0.2", {"X-Forwarded-For": "203.0.113.7"}, "203.0.113.7"),
        ("fd00::2", {"X-Forwarded-For": "2001:db8::1"}, "2001:db8::1"),
        ("10.0.0.2", {"X-Forwarded-For": "not-an-ip"}, "10.0.0.2"),
        ("10.0.0.2", {"X-Forwarded-For": "203.0.113.7, <script>"}, "10.0.0.2"),
        ("10.0.0.2", {}, "10.0.0.2"),
        # Untrusted peers: the header is ignored, the peer is normalised.
        ("192.0.2.1", {"X-Forwarded-For": "203.0.113.7"}, "192.0.2.1"),
        ("::ffff:192.0.2.1", {"X-Forwarded-For": "203.0.113.7"}, "192.0.2.1"),
        ("2001:db8::9", {"X-Forwarded-For": "203.0.113.7"}, "2001:db8::9"),
    ],
)
def test_client_address_seen_by_routes(
    data_dir: Path, peer: str, headers: dict[str, str], expected: str
) -> None:
    with make_client(data_dir, peer) as client:
        assert client.get("/test/whoami", headers=headers).json()["client"] == expected


def test_repeated_forwarded_for_headers_are_one_chain(data_dir: Path) -> None:
    with make_client(data_dir, "10.0.0.2") as client:
        response = client.get(
            "/test/whoami",
            headers=[("X-Forwarded-For", "198.51.100.1"), ("X-Forwarded-For", "203.0.113.7")],
        )
    assert response.json()["client"] == "203.0.113.7"


@pytest.mark.parametrize(
    ("peer", "proto", "expected"),
    [
        ("10.0.0.2", "https", "https"),
        ("10.0.0.2", "http, https", "https"),
        ("10.0.0.2", "HTTPS", "https"),
        ("10.0.0.2", "gopher", "http"),
        ("192.0.2.1", "https", "http"),
    ],
)
def test_forwarded_proto_only_from_trusted_proxies(
    data_dir: Path, peer: str, proto: str, expected: str
) -> None:
    with make_client(data_dir, peer) as client:
        response = client.get("/test/whoami", headers={"X-Forwarded-Proto": proto})
    assert response.json()["scheme"] == expected


def test_without_trusted_proxies_nothing_is_honoured(data_dir: Path) -> None:
    with make_client(data_dir, "10.0.0.2", trusted=None) as client:
        response = client.get(
            "/test/whoami", headers={"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"}
        )
    assert response.json() == {"client": "10.0.0.2", "scheme": "http"}


def test_untrusted_private_peer_sending_forwarded_for_is_warned_once(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with make_client(data_dir, "172.18.0.5") as client, caplog.at_level(logging.WARNING):
        for _ in range(3):
            client.get("/test/whoami", headers={"X-Forwarded-For": "203.0.113.7"})
    warnings = [r for r in caplog.records if "TINDEERR_TRUSTED_PROXIES" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].__dict__["peer"] == "172.18.0.5"


def test_public_untrusted_peer_is_not_warned_about(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # 192.0.2.0/24 and friends count as private for ipaddress: use a real public address.
    with make_client(data_dir, "8.8.8.8") as client, caplog.at_level(logging.WARNING):
        client.get("/test/whoami", headers={"X-Forwarded-For": "203.0.113.7"})
    assert "TINDEERR_TRUSTED_PROXIES" not in caplog.text


def test_unparsable_peer_is_left_alone(data_dir: Path) -> None:
    with make_client(data_dir, "testclient") as client:
        response = client.get("/test/whoami", headers={"X-Forwarded-For": "203.0.113.7"})
    assert response.json()["client"] == "testclient"


def test_access_log_has_the_forwarded_client(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        make_client(data_dir, "10.0.0.2") as client,
        caplog.at_level(logging.INFO, logger="tindeerr.access"),
    ):
        client.get("/test/whoami", headers={"X-Forwarded-For": "203.0.113.7"})
    (record,) = [r for r in caplog.records if r.name == "tindeerr.access"]
    assert record.__dict__["client"] == "203.0.113.7"
