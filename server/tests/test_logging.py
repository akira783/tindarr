import io
import json
import logging
from collections.abc import Iterator

import pytest

from tindeerr.core.logs import (
    REDACTED,
    build_handler,
    configure_logging,
    is_sensitive_key,
    redact_text,
    register_secret,
    request_id_var,
)


@pytest.fixture
def capture() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    stream = io.StringIO()
    logger = logging.getLogger("tests.capture")
    logger.handlers = [build_handler(stream)]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    yield logger, stream
    logger.handlers = []


def lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_records_are_json_with_context(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = capture
    token = request_id_var.set("req-1")
    try:
        logger.info("hello %s", "world", extra={"user_id": "u1", "count": 3})
    finally:
        request_id_var.reset(token)
    (entry,) = lines(stream)
    assert entry["message"] == "hello world"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "tests.capture"
    assert entry["request_id"] == "req-1"
    assert entry["user_id"] == "u1"
    assert entry["count"] == 3
    assert str(entry["time"]).endswith("+00:00")


def test_extras_cannot_override_base_fields(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = capture
    logger.info("real", extra={"level": "fake"})
    assert lines(stream)[0]["level"] == "INFO"


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "api_key",
        "apiKey",
        "X-Api-Key",
        "Authorization",
        "access_token",
        "refreshToken",
        "token",
        "x_plex_token",
        "cookie",
        "secret_key",
        "client_secret",
    ],
)
def test_sensitive_keys(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize("key", ["input_tokens", "user_id", "path", "status", "bypass_count"])
def test_ordinary_keys(key: str) -> None:
    assert not is_sensitive_key(key)


def test_sensitive_extra_fields_are_redacted(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = capture
    logger.info(
        "connector call",
        extra={
            "api_key": "abcdef123456",
            "headers": {"Authorization": "Bearer abc.def.ghi", "Accept": "application/json"},
            "attempts": [{"password": "hunter22"}],
            "input_tokens": 42,
        },
    )
    entry = lines(stream)[0]
    assert entry["api_key"] == REDACTED
    assert entry["headers"] == {"Authorization": REDACTED, "Accept": "application/json"}
    assert entry["attempts"] == [{"password": REDACTED}]
    assert entry["input_tokens"] == 42
    assert "hunter22" not in stream.getvalue()
    assert "abc.def.ghi" not in stream.getvalue()


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGci"),
        ("header authorization=Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpw"),
        ("GET /library?api_key=0123456789abcdef&limit=5", "0123456789abcdef"),
        ("GET /library?X-Plex-Token=PLEXTOKEN123 failed", "PLEXTOKEN123"),
        ('body {"password": "hunter22", "user": "bob"}', "hunter22"),
        ("connecting to https://admin:s3cr3tpass@jellyfin.lan/", "s3cr3tpass"),
        ('MediaBrowser Client="x", Token="abcdef123456"', "abcdef123456"),
    ],
)
def test_credentials_in_text_are_redacted(text: str, leak: str) -> None:
    redacted = redact_text(text)
    assert leak not in redacted
    assert REDACTED in redacted


def test_ordinary_text_is_untouched() -> None:
    text = "batch generated for user u1 in 1234 ms (20 cards)"
    assert redact_text(text) == text


def test_registered_secrets_are_redacted_everywhere(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = capture
    secret = "sk-very-secret-value-0001"
    register_secret(secret)
    logger.warning("provider said %s", f"invalid key {secret}", extra={"detail": f"k={secret}"})
    try:
        raise RuntimeError(f"upstream rejected {secret}")  # noqa: TRY301
    except RuntimeError:
        logger.exception("call failed")
    output = stream.getvalue()
    assert secret not in output
    assert output.count(REDACTED) >= 3
    exception = lines(stream)[1]["exception"]
    assert isinstance(exception, str)
    assert "upstream rejected" in exception


def test_short_values_are_not_registered() -> None:
    register_secret("abc")
    assert redact_text("abc") == "abc"


def test_stack_info_is_redacted(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = capture
    register_secret("stack-secret-000")
    stack_secret = "stack-secret-000"
    logger.info("with stack %s", stack_secret, stack_info=True)
    entry = lines(stream)[0]
    assert "stack" in entry
    assert stack_secret not in stream.getvalue()


def test_configure_logging_routes_everything_through_one_handler() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        configure_logging("WARNING")
        assert len(root.handlers) == 1
        assert root.level == logging.WARNING
        assert logging.getLogger("uvicorn.access").disabled
        assert logging.getLogger("uvicorn.error").propagate
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        logging.getLogger("uvicorn.access").disabled = False
        logging.captureWarnings(capture=False)
