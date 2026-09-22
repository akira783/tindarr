"""Structured JSON logging with secret redaction.

Every record goes through ``RedactionFilter`` before it is formatted. The filter:

- replaces the value of any structured field whose name looks sensitive (``password``,
  ``api_key``, ``authorization``, ``*_token``...), at any depth;
- scrubs free text (message, string fields, tracebacks): ``Bearer``/``Basic``
  credentials, ``name=value`` / ``name: value`` pairs with a sensitive name, and
  passwords in URLs;
- replaces every value registered with ``register_secret`` (the master key, secret
  settings as they are read or written) wherever it appears.
"""

import json
import logging
import re
import sys
import threading
import traceback
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Final, TextIO, cast, override

REDACTED: Final = "[REDACTED]"

#: Shortest value ``register_secret`` accepts; shorter ones would redact ordinary words.
MIN_SECRET_LENGTH: Final = 8

#: Request id of the request being handled, added to every record logged meanwhile.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Field names are compared lower-cased with separators removed, so ``apiKey``,
# ``api_key`` and ``X-Api-Key`` all match.
_SENSITIVE_PARTS: Final = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "privatekey",
)
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic)\s+(?!\[REDACTED\])[A-Za-z0-9._~+/=-]{8,}")
_PAIR = re.compile(
    r"(?i)\b([\w-]*(?:password|passwd|secret|api[_-]?key|apikey|token|authorization|cookie"
    r"|credential)[\w-]*)"
    r"(\"?'?\s*[:=]\s*\"?'?)"
    r"(?!\[REDACTED\])([^\s\"',;&}\]]+)"
)
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")

# Attributes every LogRecord has; anything else was passed through ``extra``.
# ``color_message`` is uvicorn's ANSI-coloured duplicate of the message.
_RECORD_ATTRS: Final = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
    | {"message", "asctime", "color_message"}
)

_secrets_lock = threading.Lock()
_secrets: set[str] = set()


def register_secret(value: str) -> None:
    """Redact ``value`` from every future log line. Values shorter than 8 are ignored."""
    if len(value) >= MIN_SECRET_LENGTH:
        with _secrets_lock:
            _secrets.add(value)


def clear_registered_secrets() -> None:
    """Forget registered secrets (tests only)."""
    with _secrets_lock:
        _secrets.clear()


def is_sensitive_key(name: str) -> bool:
    """Tell whether a field called ``name`` holds a credential."""
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return normalized.endswith("token") or any(part in normalized for part in _SENSITIVE_PARTS)


def redact_text(text: str) -> str:
    """Scrub credentials from free text."""
    with _secrets_lock:
        known = sorted(_secrets, key=len, reverse=True)
    for secret in known:
        text = text.replace(secret, REDACTED)
    text = _URL_USERINFO.sub(rf"\1{REDACTED}@", text)
    text = _AUTH_SCHEME.sub(rf"\1 {REDACTED}", text)
    return _PAIR.sub(rf"\1\2{REDACTED}", text)


def redact_value(value: object) -> object:
    """Redact a structured value: sensitive keys in mappings, credentials in strings."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        return {
            str(key): REDACTED if is_sensitive_key(str(key)) else redact_value(item)
            for key, item in mapping.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [redact_value(item) for item in cast("Sequence[object]", value)]
    return value


class RedactionFilter(logging.Filter):
    """Redacts the message, the ``extra`` fields and the traceback of each record."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = None
        for key in set(vars(record)) - _RECORD_ATTRS:
            value: object = getattr(record, key)
            setattr(record, key, REDACTED if is_sensitive_key(key) else redact_value(value))
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        record.exc_info = None
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, request id, extra fields."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            entry["request_id"] = request_id
        for key in sorted(set(vars(record)) - _RECORD_ATTRS):
            entry.setdefault(key, getattr(record, key))
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            entry["exception"] = record.exc_text
        if record.stack_info:
            entry["stack"] = record.stack_info
        return json.dumps(entry, default=str, ensure_ascii=False)


def build_handler(stream: TextIO | None = None) -> logging.Handler:
    """Return a stream handler (stdout by default) that redacts, then writes JSON."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.addFilter(RedactionFilter())
    handler.setFormatter(JsonFormatter())
    return handler


def configure_logging(level: str) -> None:
    """Send every log record, including uvicorn's, through one redacting JSON handler."""
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(build_handler())
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # Tindeerr logs its own migration summary; Alembic's step-by-step lines are noise.
    logging.getLogger("alembic").setLevel(logging.WARNING)
    # Requests are logged by the app itself, with the request id and without query strings.
    logging.getLogger("uvicorn.access").disabled = True
    logging.captureWarnings(capture=True)
