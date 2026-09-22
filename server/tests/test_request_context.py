"""The request context: forwarded headers, allowed hosts and origins (auth.md, §1)."""

import logging
from ipaddress import IPv4Address, ip_address, ip_network
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse
from starlette.types import Receive, Scope, Send

from tests.support import CONSOLE_HOST, server_config
from tindeerr.api.context import (
    WARNING_INTERVAL_S,
    HostPolicy,
    RequestContext,
    RequestContextMiddleware,
    client_ip,
    forwarded_client,
    request_context,
)
from tindeerr.core.net import parse_host
from tindeerr.main.app import create_app

TRUSTED = (ip_network("10.0.0.0/8"), ip_network("fd00::/8"))


def add_whoami(app: FastAPI) -> None:
    """A route that reports the context the middleware resolved."""

    async def whoami(request: Request) -> dict[str, object]:
        context = request_context(request.scope)
        return {
            "client": None if context.client is None else str(context.client),
            "peer": None if context.peer is None else str(context.peer),
            "scheme": context.scheme,
            "host": None if context.host is None else context.host.host,
            "origin": context.origin,
            "private": context.client_is_private,
            "rate_limit_key": context.rate_limit_key,
            "trusted_peer": context.trusted_peer,
        }

    app.add_api_route("/test/whoami", whoami)


def make_client(
    data_dir: Path,
    peer: str = "192.0.2.1",
    trusted: str | None = "10.0.0.0/8,fd00::/8",
    host: str = "testserver",
    **overrides: Any,
) -> TestClient:
    config = server_config(data_dir, trusted_proxies=trusted or (), **overrides)
    app = create_app(config)
    add_whoami(app)
    return TestClient(app, base_url=f"http://{host}", client=(peer, 5000))


def whoami(client: TestClient, **kwargs: Any) -> dict[str, Any]:
    response = client.get("/test/whoami", **kwargs)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- the client address -------------------------------------------------------------


@pytest.mark.parametrize(
    ("forwarded_for", "expected"),
    [
        ("203.0.113.7", "203.0.113.7"),
        # Leftmost entries come from the client: only the rightmost untrusted one counts.
        ("198.51.100.1, 203.0.113.7", "203.0.113.7"),
        ("198.51.100.1, 203.0.113.7, 10.0.0.9", "203.0.113.7"),
        ("1.1.1.1,2.2.2.2 , 203.0.113.7", "203.0.113.7"),
        # IPv6 and IPv4-mapped entries.
        ("2001:db8::1", "2001:db8::1"),
        ("2001:db8::1, fd00::5", "2001:db8::1"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("::ffff:10.0.0.3, 203.0.113.7", "203.0.113.7"),
        # Only trusted hops: the leftmost one is the best guess.
        ("10.0.0.7, 10.0.0.8", "10.0.0.7"),
        (", ,", "10.0.0.2"),
    ],
)
def test_forwarded_client(forwarded_for: str, expected: str) -> None:
    peer = IPv4Address("10.0.0.2")
    assert forwarded_client(peer, forwarded_for, TRUSTED) == ip_address(expected)


@pytest.mark.parametrize(
    "forwarded_for",
    [
        "garbage",
        "203.0.113.7, garbage",
        "198.51.100.1, garbage, 10.0.0.9",
        "203.0.113.7:4444",
        "unknown",
        "<script>",
    ],
)
def test_a_chain_that_is_not_addresses_is_reported(forwarded_for: str) -> None:
    # A misconfigured proxy: the caller falls back to the peer, never to a spoofed value.
    assert forwarded_client(IPv4Address("10.0.0.2"), forwarded_for, TRUSTED) is None


def test_client_ip_reads_the_scope() -> None:
    assert client_ip({"client": ("::ffff:192.0.2.1", 1)}) == IPv4Address("192.0.2.1")
    assert client_ip({"client": ("not-an-ip", 1)}) is None
    assert client_ip({"client": None}) is None
    assert client_ip({}) is None


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
        assert whoami(client, headers=headers)["client"] == expected


def test_repeated_forwarded_for_headers_are_one_chain(data_dir: Path) -> None:
    with make_client(data_dir, "10.0.0.2") as client:
        body = whoami(
            client,
            headers=[("X-Forwarded-For", "198.51.100.1"), ("X-Forwarded-For", "203.0.113.7")],
        )
    assert body["client"] == "203.0.113.7"


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
        assert whoami(client, headers={"X-Forwarded-Proto": proto})["scheme"] == expected


def test_repeated_forwarded_proto_headers_are_one_chain(data_dir: Path) -> None:
    # The nearest proxy appends last, and a client cannot prepend its way to "https".
    with make_client(data_dir, "10.0.0.2") as client:
        forged = whoami(
            client,
            headers=[("X-Forwarded-Proto", "https"), ("X-Forwarded-Proto", "http")],
        )
        real = whoami(
            client,
            headers=[("X-Forwarded-Proto", "http"), ("X-Forwarded-Proto", "https")],
        )
    assert forged["scheme"] == "http"
    assert real["scheme"] == "https"


def test_without_trusted_proxies_nothing_is_honoured(data_dir: Path) -> None:
    with make_client(data_dir, "10.0.0.2", trusted=None) as client:
        body = whoami(
            client, headers={"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"}
        )
    assert (body["client"], body["scheme"]) == ("10.0.0.2", "http")
    assert body["trusted_peer"] is False


def test_the_forwarded_host_is_never_used(data_dir: Path) -> None:
    with make_client(data_dir, "10.0.0.2") as client:
        body = whoami(
            client, headers={"X-Forwarded-Host": "evil.example", "X-Forwarded-Port": "1234"}
        )
    assert body["host"] == "testserver"
    assert body["origin"] == "http://testserver"


def test_unparsable_peer_is_left_alone(data_dir: Path) -> None:
    with make_client(data_dir, "testclient") as client:
        body = whoami(client, headers={"X-Forwarded-For": "203.0.113.7"})
    assert body["client"] is None
    assert body["rate_limit_key"] == "unknown"
    assert body["private"] is False


# --- warnings -----------------------------------------------------------------------


def test_untrusted_private_peer_sending_forwarded_headers_is_warned_once_an_hour(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        make_client(data_dir, "172.18.0.5", trusted=None) as client,
        caplog.at_level(logging.WARNING),
    ):
        for _ in range(3):
            whoami(client, headers={"X-Forwarded-For": "203.0.113.7"})
        whoami(client, headers={"X-Request-ID": "abc"})
    warnings = [r for r in caplog.records if "TINDEERR_TRUSTED_PROXIES" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].__dict__["peer"] == "172.18.0.5"
    assert "203.0.113.7" not in caplog.text


def test_the_warning_comes_back_after_an_hour(caplog: pytest.LogCaptureFixture) -> None:
    seconds = [1000.0]

    async def bare(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    middleware = RequestContextMiddleware(bare, hosts=HostPolicy(), monotonic=lambda: seconds[0])
    # No lifespan here: the middleware is driven on its own, not the whole application.
    client = TestClient(middleware, client=("172.18.0.5", 5000))
    with caplog.at_level(logging.WARNING):
        client.get("/", headers={"X-Forwarded-For": "203.0.113.7"})
        seconds[0] += WARNING_INTERVAL_S / 2
        client.get("/", headers={"X-Forwarded-For": "203.0.113.7"})
        seconds[0] += WARNING_INTERVAL_S
        client.get("/", headers={"X-Forwarded-For": "203.0.113.7"})
    warnings = [r for r in caplog.records if "TINDEERR_TRUSTED_PROXIES" in r.getMessage()]
    assert len(warnings) == 2


def test_a_misconfigured_proxy_is_warned_about(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with make_client(data_dir, "10.0.0.2") as client, caplog.at_level(logging.WARNING):
        whoami(client, headers={"X-Forwarded-For": "garbage"})
    assert "not an IP address" in caplog.text
    assert "garbage" not in caplog.text


def test_public_untrusted_peer_is_not_warned_about(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with make_client(data_dir, "8.8.8.8", trusted=None) as client, caplog.at_level(logging.WARNING):
        whoami(client, headers={"X-Forwarded-For": "203.0.113.7"})
    assert "TINDEERR_TRUSTED_PROXIES" not in caplog.text


# --- private network ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("peer", "private"),
    [("192.168.1.4", True), ("10.1.2.3", True), ("127.0.0.1", True), ("8.8.8.8", False)],
)
def test_private_is_decided_on_the_resolved_address(
    data_dir: Path, peer: str, private: bool
) -> None:
    with make_client(data_dir, peer, trusted=None) as client:
        assert whoami(client)["private"] is private


def test_behind_a_proxy_private_follows_the_forwarded_address(data_dir: Path) -> None:
    # The peer is always private behind a reverse proxy: the client is what counts.
    with make_client(data_dir, "10.0.0.2") as client:
        assert whoami(client, headers={"X-Forwarded-For": "203.0.113.7"})["private"] is False
        assert whoami(client, headers={"X-Forwarded-For": "192.168.1.4"})["private"] is True


# --- allowed hosts and origin -------------------------------------------------------


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "127.0.0.1:8787", "localhost", "localhost:8787", CONSOLE_HOST]
)
def test_allowed_hosts(data_dir: Path, host: str) -> None:
    with make_client(data_dir, host=host) as client:
        assert client.get("/api/v1/server/info").status_code == 200


@pytest.mark.parametrize("host", ["[::1]", "[2001:db8::1]:8787", "[fd00::1]"])
def test_bracketed_ipv6_hosts_are_allowed(data_dir: Path, host: str) -> None:
    with make_client(data_dir) as client:
        assert client.get("/api/v1/server/info", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize(
    "host", ["evil.example", "console.test.evil.example", "testserver.evil.example", "not a host"]
)
def test_unknown_hosts_are_refused_before_routing(data_dir: Path, host: str) -> None:
    with make_client(data_dir, host="testserver") as client:
        response = client.get("/api/v1/server/info", headers={"Host": host})
    assert response.status_code == 400
    assert response.json()["code"] == "host_not_allowed"
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_the_probe_answers_whatever_the_host_is(data_dir: Path) -> None:
    with make_client(data_dir, host="testserver") as client:
        assert client.get("/healthz", headers={"Host": "evil.example"}).status_code == 200


def test_an_unknown_host_is_refused_even_on_an_unknown_path(data_dir: Path) -> None:
    with make_client(data_dir, host="testserver") as client:
        response = client.get("/api/v1/nope", headers={"Host": "evil.example"})
    assert response.json()["code"] == "host_not_allowed"


def test_the_origin_is_built_from_the_validated_host(data_dir: Path) -> None:
    with make_client(data_dir, "10.0.0.2", host="console.test") as client:
        plain = whoami(client)
        forwarded = whoami(client, headers={"X-Forwarded-Proto": "https"})
    assert plain["origin"] == "http://console.test"
    assert forwarded["origin"] == "https://console.test"


def test_the_public_url_host_is_accepted_once_it_is_set() -> None:
    hosts = HostPolicy(("console.test",))
    assert not hosts.allows(parse_host("tindeerr.example.com"))
    hosts.set_public_url("https://tindeerr.example.com:8443")
    assert hosts.public_url_host == "tindeerr.example.com"
    assert hosts.allows(parse_host("tindeerr.example.com"))
    assert hosts.allows(parse_host("tindeerr.example.com:9000"))  # the port never matters
    hosts.set_public_url(None)
    assert not hosts.allows(parse_host("tindeerr.example.com"))
    hosts.set_public_url("not a url")
    assert hosts.public_url_host is None


def test_every_ip_literal_and_localhost_are_allowed() -> None:
    hosts = HostPolicy()
    for value in ("192.168.1.4", "8.8.8.8:8787", "[fd00::1]", "localhost", "LOCALHOST:8787"):
        assert hosts.allows(parse_host(value)), value
    assert not hosts.allows(None)
    assert not hosts.allows(parse_host("example.com"))


def test_context_without_a_valid_host_has_no_origin() -> None:
    context = RequestContext(
        peer=None, client=None, scheme="https", host=None, host_allowed=False, trusted_peer=False
    )
    assert context.origin is None
    assert context.rate_limit_key == "unknown"
    assert context.is_loopback_to_localhost is False
