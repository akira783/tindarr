import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from tindeerr import __version__
from tindeerr.core.config import ServerConfig
from tindeerr.main.app import create_app
from tindeerr.storage.db import database_path

SECURITY_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}


def add_test_routes(app: FastAPI) -> None:
    async def items(limit: int) -> dict[str, int]:
        return {"limit": limit}

    async def boom() -> None:
        msg = "database password=hunter22 exploded"
        raise RuntimeError(msg)

    async def whoami(request: Request) -> dict[str, str | None]:
        return {"client": request.client.host if request.client else None}

    app.add_api_route("/test/items", items)
    app.add_api_route("/test/boom", boom)
    app.add_api_route("/test/whoami", whoami)


@pytest.fixture
def test_client(config: ServerConfig) -> TestClient:
    app = create_app(config)
    add_test_routes(app)
    return TestClient(app)


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert "server" not in response.headers


def test_server_info_on_a_fresh_install(client: TestClient) -> None:
    response = client.get("/api/v1/server/info")
    assert response.status_code == 200
    assert response.json() == {
        "name": "Tindeerr",
        "version": __version__,
        "api_version": 1,
        "min_app_version": "0.1.0",
        "setup_required": True,
        "media_server": None,
        "auth_methods": [],
        "capabilities": [],
        "tmdb_image_base_url": "https://image.tmdb.org/t/p/",
    }


@pytest.mark.parametrize(
    ("kind", "methods"),
    [("jellyfin", ["password"]), ("emby", ["password"]), ("plex", ["plex_pin"])],
)
def test_server_info_reflects_the_media_server(
    config: ServerConfig, monkeypatch: pytest.MonkeyPatch, kind: str, methods: list[str]
) -> None:
    monkeypatch.setenv("TINDEERR_MEDIA_SERVER_KIND", kind)
    monkeypatch.setenv("TINDEERR_SERVER_NAME", "Chez nous")
    with TestClient(create_app(config)) as client:
        body = client.get("/api/v1/server/info").json()
    assert body["media_server"] == {"kind": kind}
    assert body["auth_methods"] == methods
    assert body["name"] == "Chez nous"


def test_unknown_media_server_kind_is_ignored(
    config: ServerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TINDEERR_MEDIA_SERVER_KIND", "kodi")
    with TestClient(create_app(config)) as client:
        body = client.get("/api/v1/server/info").json()
    assert body["media_server"] is None
    assert body["auth_methods"] == []


def test_setup_required_follows_the_state_flag(config: ServerConfig, data_dir: Path) -> None:
    with TestClient(create_app(config)) as client:
        assert client.get("/api/v1/server/info").json()["setup_required"] is True
        with closing(sqlite3.connect(database_path(data_dir))) as connection, connection:
            connection.execute(
                "UPDATE server_state SET setup_completed_at = ?",
                (datetime.now(UTC).replace(tzinfo=None).isoformat(" "),),
            )
        assert client.get("/api/v1/server/info").json()["setup_required"] is False


def test_request_id_is_generated_or_echoed(client: TestClient) -> None:
    generated = client.get("/healthz").headers["x-request-id"]
    assert len(generated) == 32
    assert (
        client.get("/healthz", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"]
        == "abc-123"
    )
    unsafe = client.get("/healthz", headers={"X-Request-ID": "bad id\x7f" + "x" * 200})
    assert unsafe.headers["x-request-id"] != "bad id"
    assert len(unsafe.headers["x-request-id"]) == 32


def test_access_log_has_request_id_and_no_query_string(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="tindeerr.access"):
        client.get("/api/v1/server/info?api_key=leaky", headers={"X-Request-ID": "req-42"})
    (record,) = [r for r in caplog.records if r.name == "tindeerr.access"]
    assert record.__dict__["path"] == "/api/v1/server/info"
    assert record.__dict__["status"] == 200
    assert "leaky" not in caplog.text


def test_unknown_path_is_a_not_found_problem(client: TestClient) -> None:
    response = client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "code": "not_found",
    }


def test_wrong_method_is_a_problem_with_allow_header(client: TestClient) -> None:
    response = client.post("/healthz")
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"
    assert response.headers["allow"] == "GET"


def test_invalid_input_is_a_validation_problem(test_client: TestClient) -> None:
    with test_client:
        response = test_client.get("/test/items", params={"limit": "secret-looking-input"})
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["errors"] == [
        {
            "field": "query.limit",
            "message": "Input should be a valid integer, unable to parse string as an integer",
        }
    ]
    assert "secret-looking-input" not in response.text


def test_unhandled_error_leaks_nothing(
    test_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with test_client, caplog.at_level(logging.ERROR):
        response = test_client.get("/test/boom")
    assert response.status_code == 500
    assert response.json() == {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
        "code": "internal_error",
    }
    assert "hunter22" not in response.text
    assert "exploded" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "x-request-id" in response.headers
    assert any(record.exc_info for record in caplog.records)


def test_docs_are_disabled_by_default(client: TestClient) -> None:
    assert client.get("/api/docs").status_code == 404
    assert client.get("/api/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404


def test_docs_can_be_enabled(data_dir: Path) -> None:
    with TestClient(create_app(ServerConfig(data_dir=data_dir, api_docs=True))) as client:
        docs = client.get("/api/docs")
        assert docs.status_code == 200
        assert "content-security-policy" not in docs.headers
        assert client.get("/api/openapi.json").status_code == 200


def test_forwarded_headers_are_ignored_without_trusted_proxies(config: ServerConfig) -> None:
    app = create_app(config)
    add_test_routes(app)
    with TestClient(app, client=("10.0.0.2", 5000)) as client:
        response = client.get("/test/whoami", headers={"X-Forwarded-For": "203.0.113.7"})
    assert response.json() == {"client": "10.0.0.2"}


@pytest.mark.parametrize(
    ("peer", "expected"),
    [("10.0.0.2", "203.0.113.7"), ("192.0.2.1", "192.0.2.1")],
)
def test_forwarded_headers_are_honoured_only_from_trusted_proxies(
    data_dir: Path, peer: str, expected: str
) -> None:
    config = ServerConfig(data_dir=data_dir, trusted_proxies="10.0.0.0/8")  # pyright: ignore[reportArgumentType]
    app = create_app(config)
    add_test_routes(app)
    with TestClient(app, client=(peer, 5000)) as client:
        response = client.get(
            "/test/whoami", headers={"X-Forwarded-For": "198.51.100.1, 203.0.113.7"}
        )
    assert response.json() == {"client": expected}
