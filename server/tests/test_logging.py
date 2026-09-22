import io
import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from tindarr.core.logs import (
    MAX_TEXT_LENGTH,
    REDACTED,
    TRUNCATED,
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


# --- ReDoS and truncation -------------------------------------------------------------

ADVERSARIAL_INPUTS = [
    "a-" * 8192,
    "a_" * 8192,
    "a" * 16384,
    "password" * 2048,
    "token-" * 2731,
    "password=" * 1820,
    "https://" + "x:" * 8188,
    "://" + "@" * 16381,
    "Bearer " * 2340,
    "=" * 16384,
    " " * 16384,
    '"' * 16384,
]


@pytest.mark.parametrize("text", ADVERSARIAL_INPUTS, ids=lambda text: repr(text[:12]))
def test_redaction_is_linear_on_adversarial_input(text: str) -> None:
    started = time.perf_counter()
    redact_text(text, max_length=len(text))
    assert time.perf_counter() - started < 0.1


def test_long_text_is_truncated_before_redaction() -> None:
    text = "x" * 10_000 + " password=hunter22"
    redacted = redact_text(text)
    assert len(redacted) < MAX_TEXT_LENGTH + 100
    assert redacted.endswith(TRUNCATED)
    assert "hunter22" not in redacted


def test_truncation_never_leaves_part_of_a_credential() -> None:
    text = "x " * ((MAX_TEXT_LENGTH - 10) // 2) + "Bearer abcdefghijklmnopqrstuvwxyz"
    redacted = redact_text(text)
    assert "abc" not in redacted.removesuffix(TRUNCATED).split()[-1]


def test_registered_secret_across_the_truncation_point_is_redacted() -> None:
    secret = "sk-registered-secret-0001"
    register_secret(secret)
    text = "x" * (MAX_TEXT_LENGTH - 5) + secret
    assert "sk-re" not in redact_text(text)


def test_access_log_of_a_huge_path_is_fast(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = capture
    started = time.perf_counter()
    logger.info("request", extra={"path": "/" + "a-" * 8192})
    assert time.perf_counter() - started < 0.1
    (entry,) = lines(stream)
    assert len(str(entry["path"])) < MAX_TEXT_LENGTH + 100


def test_tracebacks_keep_more_context_than_messages(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = capture
    try:
        raise RuntimeError("y" * 5000)  # noqa: TRY301
    except RuntimeError:
        logger.exception("failed")
    exception = lines(stream)[0]["exception"]
    assert isinstance(exception, str)
    assert "y" * 5000 in exception


# --- redaction gaps -------------------------------------------------------------------


class _BoomError(Exception):
    pass


@dataclass
class _Connector:
    url: str
    note: str


def test_non_primitive_extras_are_redacted_through_str(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = capture
    logger.warning(
        "m",
        extra={
            "err": _BoomError("password=hunter22"),
            "tags": {"token=abcdefgh123"},
            "pair": ("ok", "secret=s3cr3tvalue"),
            "conn": _Connector("https://admin:pa55word@jf.lan/", "api_key=K3Y0123456"),
            "raw": b"password=bytes-leak",
        },
    )
    output = stream.getvalue()
    for leak in ("hunter22", "abcdefgh123", "s3cr3tvalue", "pa55word", "K3Y0123456", "bytes-leak"):
        assert leak not in output
    entry = lines(stream)[0]
    assert entry["pair"] == ["ok", f"secret={REDACTED}"]


def test_deeply_nested_values_do_not_recurse_forever(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = capture
    nested: list[object] = []
    nested.append(nested)
    logger.info("m", extra={"nested": nested})
    assert lines(stream)[0]["nested"]


@pytest.mark.parametrize(
    "key",
    [
        "session",
        "session_id",
        "sessionToken",
        "sid",
        "jwt",
        "csrf",
        "X-CSRF-Token",
        "otp",
        "pin",
        "code",
        "pairing_code",
        "setup_code",
        "refresh",
        "refresh_token",
        "secret",
        "token",
        "key",
        "sig",
        "signature",
    ],
)
def test_more_sensitive_keys(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize(
    "key", ["status_code", "keyword", "pinned", "inside", "considered", "monkey", "otpx_count"]
)
def test_short_names_only_match_whole_keys(key: str) -> None:
    assert not is_sensitive_key(key)


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("GET /maps?key=GOOGLEKEY123&sig=SIG123abc", "GOOGLEKEY123"),
        ("GET /maps?key=GOOGLEKEY123&sig=SIG123abc", "SIG123abc"),
        ("callback?code=OAUTHCODE1&state=x", "OAUTHCODE1"),
        ("session_id=S1D0123456 expired", "S1D0123456"),
        ("sid=S1D0123456", "S1D0123456"),
        ("jwt=eyJxx.yy.zz", "eyJxx"),
        ("X-CSRF-Token: CSRFVALUE12", "CSRFVALUE12"),
        ("otp=123456", "123456"),
        ("pin=4242", "4242"),
        ("pairing_code=PAIRCODE0123", "PAIRCODE0123"),
        ("refresh=R3FR3SH0123", "R3FR3SH0123"),
        ("clientSecret=CAMELSECRET1", "CAMELSECRET1"),
        ('password: "hunter 22 zz"', "22 zz"),
        ("password='hunter 22 zz'", "22 zz"),
        ("password=hunter 22, retrying", "22"),
        ("Authorization: Bearer abcdefgh12345 extra", "abcdefgh12345"),
        ("url https://ghp_TOKENONLY123@github.com/x", "ghp_TOKENONLY123"),
        ("http://user:p@ss@host.lan/path", "ss@host"),
        ("http://user:p@ss@host.lan/path", "p@"),
        ("GET /x?secret=QSECRET0123", "QSECRET0123"),
        ("GET /x?api_key=QAPIKEY0123", "QAPIKEY0123"),
        ("GET /x?ApiKey=QAPIKEY0123", "QAPIKEY0123"),
        ("GET /x?apikey=QAPIKEY0123", "QAPIKEY0123"),
        ("GET /library/sections?X-Plex-Token=PLEXTOKEN123", "PLEXTOKEN123"),
        ("GET /x?access_token=ACCESS01234", "ACCESS01234"),
    ],
)
def test_more_credentials_in_text_are_redacted(text: str, leak: str) -> None:
    redacted = redact_text(text)
    assert leak not in redacted
    assert REDACTED in redacted


@pytest.mark.parametrize(
    "text",
    [
        "HTTP status code: 404",
        "exit code 1",
        "migrated database to revision 0002",
        "https://jellyfin.lan/web/index.html",
        "keyword search for 'dune' returned 20 results",
        "20 input_tokens used",
    ],
)
def test_ordinary_text_stays_readable(text: str) -> None:
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "password=hunter22 and https://tok@host/ and Bearer abcdefgh1234",
        "Authorization: Bearer abcdefgh1234",
        'body {"password": "hunter 22", "key": "value"}',
        "GET /x?key=abc&sig=def",
    ],
)
def test_redaction_is_idempotent(text: str) -> None:
    once = redact_text(text)
    assert once != text
    assert redact_text(once) == once


def test_http_client_loggers_are_quiet_by_default() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        configure_logging("DEBUG")
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        logging.getLogger("uvicorn.access").disabled = False
        logging.captureWarnings(capture=False)
