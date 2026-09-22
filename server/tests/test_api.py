import logging
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from tindeerr import __version__
from tindeerr.core.config import ConfigError, ServerConfig
from tindeerr.core.crypto import DecryptionError
from tindeerr.core.errors import ProblemError
from tindeerr.main.app import create_app
from tindeerr.storage.db import database_path
from tindeerr.storage.settings import SettingLockedError

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

    async def locked() -> None:
        raise SettingLockedError("server_name")

    async def undecryptable() -> None:
        msg = "encrypted with another key (id 0a1b2c3d); was the secret key changed?"
        raise DecryptionError(msg)

    async def limited() -> None:
        raise ProblemError(429, "rate_limited", "Too many attempts.", {"Retry-After": "3"})

    async def unavailable() -> None:
        raise ProblemError(503, "media_server_unreachable", "at http://10.0.0.5:8096 (secret)")

    async def framework(status: int) -> None:
        raise StarletteHTTPException(status)

    async def streaming() -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            yield b"first chunk"
            msg = "failed mid-stream"
            raise RuntimeError(msg)

        return StreamingResponse(chunks())

    app.add_api_route("/test/items", items)
    app.add_api_route("/test/boom", boom)
    app.add_api_route("/test/whoami", whoami)
    app.add_api_route("/test/locked", locked)
    app.add_api_route("/test/undecryptable", undecryptable)
    app.add_api_route("/test/limited", limited)
    app.add_api_route("/test/unavailable", unavailable)
    app.add_api_route("/test/framework/{status}", framework)
    app.add_api_route("/test/streaming", streaming)


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


def test_unknown_media_server_kind_prevents_startup(
    config: ServerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TINDEERR_MEDIA_SERVER_KIND", "kodi")
    with (
        pytest.raises(ConfigError, match="TINDEERR_MEDIA_SERVER_KIND"),
        TestClient(create_app(config)),
    ):
        pass


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


def test_huge_hyphenated_path_does_not_block_the_server(client: TestClient) -> None:
    # Regression: the access log's redaction was quadratic on such paths (16 s for 16 KB).
    started = time.perf_counter()
    response = client.get("/" + "a-" * 8192)
    assert response.status_code == 404
    assert time.perf_counter() - started < 1


def test_setting_locked_is_a_409_problem(test_client: TestClient) -> None:
    with test_client:
        response = test_client.get("/test/locked")
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Conflict",
        "status": 409,
        "code": "setting_locked",
        "detail": "server_name is set by TINDEERR_SERVER_NAME and cannot be changed here",
    }


def test_decryption_error_is_a_generic_500_logged_distinctly(
    test_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with test_client, caplog.at_level(logging.ERROR):
        response = test_client.get("/test/undecryptable")
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "another key" not in response.text
    (record,) = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "secret key changed" in record.getMessage()
    assert record.__dict__["problem"] == "decryption_failed"


def test_problem_error_keeps_status_code_detail_and_headers(test_client: TestClient) -> None:
    with test_client:
        response = test_client.get("/test/limited")
    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"
    assert response.json()["detail"] == "Too many attempts."
    assert response.headers["retry-after"] == "3"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_server_side_problem_details_are_logged_not_sent(
    test_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with test_client, caplog.at_level(logging.ERROR):
        response = test_client.get("/test/unavailable")
    assert response.status_code == 503
    assert response.json() == {
        "type": "about:blank",
        "title": "Service Unavailable",
        "status": 503,
        "code": "media_server_unreachable",
    }
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "validation_error"),
        (401, "unauthorized"),
        (403, "forbidden"),
        (404, "not_found"),
        (405, "method_not_allowed"),
        (409, "bad_request"),
        (429, "rate_limited"),
        (502, "internal_error"),
    ],
)
def test_framework_errors_get_a_code_from_their_status(
    test_client: TestClient, status: int, code: str
) -> None:
    with test_client:
        response = test_client.get(f"/test/framework/{status}")
    assert response.status_code == status
    assert response.json()["code"] == code


def test_error_after_the_response_started_aborts_the_connection(test_client: TestClient) -> None:
    with test_client, pytest.raises(RuntimeError, match="mid-stream"):
        test_client.get("/test/streaming")
