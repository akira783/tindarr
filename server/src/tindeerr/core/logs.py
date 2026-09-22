"""Structured JSON logging with secret redaction.

Every record goes through ``RedactionFilter`` before it is formatted. The filter:

- replaces the value of any structured field whose name looks sensitive (``password``,
  ``api_key``, ``authorization``, ``session``, ``*_token``...), at any depth;
- turns every other value into JSON-safe data, going through ``str()`` for anything that
  is not a string, a number, a boolean, ``None``, a mapping or a sequence (exceptions,
  URLs, dataclasses...), so nothing reaches the output unredacted;
- scrubs free text (message, string fields, tracebacks): ``Bearer``/``Basic``
  credentials, ``name=value`` / ``name: value`` pairs with a sensitive name, and user
  info in URLs;
- replaces every value registered with ``register_secret`` (the master key, secret
  settings as they are read or written) wherever it appears.

Free text is truncated before the patterns run (``MAX_TEXT_LENGTH``, more for
tracebacks), and every pattern is bounded, so an attacker-controlled string (a request
path, say) costs linear time to redact.
"""

import json
import logging
import re
import sys
import threading
import traceback
from collections.abc import Mapping, Sequence, Set
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Final, TextIO, cast, override

REDACTED: Final = "[REDACTED]"
TRUNCATED: Final = "...[truncated]"

#: Longest free text kept in a log field or message, in characters.
MAX_TEXT_LENGTH: Final = 2048
#: Longest traceback or stack kept, in characters.
MAX_TRACEBACK_LENGTH: Final = 16384
#: Nesting depth beyond which structured values are replaced by a placeholder.
MAX_DEPTH: Final = 8
_TOO_DEEP: Final = "[too deep]"
#: How far back a truncation looks for a word boundary, so it does not keep the first
#: characters of a credential it cut through.
_WORD_BOUNDARY_LOOKBACK: Final = 256

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
    "session",
    "csrf",
    "refresh",
    "signature",
    "pairingcode",
    "setupcode",
    "codeverifier",
    "codechallenge",
    "nonce",
)
# Short names that are only sensitive as a whole name: ``code`` is, ``status_code`` is not.
_SENSITIVE_NAMES: Final = frozenset(
    {"sid", "jwt", "otp", "pin", "code", "key", "sig", "handle", "pinid"}
)

# ``name=value`` or ``name: value`` in free text. Long names match anywhere in a word
# (``clientSecret``, ``X-Plex-Token``) and take ``=`` or ``:``; their value runs to the
# closing quote or, unquoted, to a delimiter, spaces included (over-redacting is fine).
# Short names must be the whole word and take ``=`` only (query strings, forms), so
# "status code: 404" stays readable. Every repetition is bounded: no ReDoS.
_LONG_PAIR = re.compile(
    r"(?i)((?:password|passwd|passphrase|secret|api[_-]?key|token|authorization|cookie"
    r"|credential|session|csrf|refresh|pairing[_-]?code|setup[_-]?code|nonce"
    r"|code[_-]?verifier|code[_-]?challenge|handle)[\w-]{0,32}"
    r"[\"']?\s{0,8}[:=]\s{0,8})"
    r"(?!\s{0,8}[\"']?\[REDACTED\])"
    r"(\"[^\"\r\n]{1,512}|'[^'\r\n]{1,512}|(?:\[REDACTED\]|[^\r\n,;&\"'}\]]){1,512})"
)
_SHORT_PAIR = re.compile(
    r"(?i)((?<![\w-])(?:key|sig|sid|jwt|otp|pin|code)\s{0,8}=\s{0,8})"
    r"(?!\s{0,8}[\"']?\[REDACTED\])"
    r"(\"[^\"\r\n]{1,512}|'[^'\r\n]{1,512}|(?:\[REDACTED\]|[^\s&;,\"'}\]]){1,512})"
)
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic)\s{1,8}(?!\[REDACTED\])[A-Za-z0-9._~+/=-]{8,4096}")
# Everything between ``://`` and the last ``@`` before the host: a token alone
# (``https://TOKEN@host``), or user and password, even a password containing ``@``.
_URL_USERINFO = re.compile(r"://(?!\[REDACTED\]@)[^/\s?#]{1,256}@")
_WHITESPACE = re.compile(r"\s")

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
    return (
        normalized.endswith("token")
        or normalized in _SENSITIVE_NAMES
        or any(part in normalized for part in _SENSITIVE_PARTS)
    )


def _truncate(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    kept = text[:max_length]
    if not text[max_length].isspace():
        # Cut at the last word boundary, so no leading part of a credential survives.
        tail = kept[-_WORD_BOUNDARY_LOOKBACK:]
        boundary = max((match.end() for match in _WHITESPACE.finditer(tail)), default=0)
        if boundary:
            kept = kept[: len(kept) - len(tail) + boundary]
    return kept + TRUNCATED


def _redact_pair_value(match: re.Match[str]) -> str:
    # Keep the name, the separator and an opening quote: ``password: "[REDACTED]"``.
    value = match[2]
    quote = value[0] if value[0] in "\"'" else ""
    return f"{match[1]}{quote}{REDACTED}"


def redact_text(text: str, *, max_length: int = MAX_TEXT_LENGTH) -> str:
    """Scrub credentials from free text, truncated to ``max_length`` characters first.

    Registered secrets are replaced before the truncation, so one cut in half cannot
    survive; the patterns run after it, on bounded input.
    """
    with _secrets_lock:
        known = sorted(_secrets, key=len, reverse=True)
    for secret in known:
        text = text.replace(secret, REDACTED)
    text = _truncate(text, max_length)
    text = _URL_USERINFO.sub(f"://{REDACTED}@", text)
    text = _AUTH_SCHEME.sub(rf"\1 {REDACTED}", text)
    text = _LONG_PAIR.sub(_redact_pair_value, text)
    return _SHORT_PAIR.sub(_redact_pair_value, text)


def redact_value(value: object, _depth: int = 0) -> object:
    """Return ``value`` as JSON-safe data with every credential redacted.

    Strings are scrubbed; mappings lose the values of sensitive keys; sequences and sets
    become lists; numbers, booleans and ``None`` are kept; anything else is scrubbed as
    ``str(value)``.
    """
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if _depth >= MAX_DEPTH:
        return _TOO_DEEP
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        return {
            str(key): REDACTED if is_sensitive_key(str(key)) else redact_value(item, _depth + 1)
            for key, item in mapping.items()
        }
    if isinstance(value, Sequence | Set) and not isinstance(value, bytes | bytearray):
        items = cast("Sequence[object] | Set[object]", value)
        return [redact_value(item, _depth + 1) for item in items]
    return redact_text(str(value))


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
            record.exc_text = redact_text(record.exc_text, max_length=MAX_TRACEBACK_LENGTH)
        record.exc_info = None
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info, max_length=MAX_TRACEBACK_LENGTH)
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


def quiet_noisy_libraries() -> None:
    """Raise the level of libraries that would print credentials or noise.

    HTTP clients log full URLs (query strings included) at INFO and DEBUG, and the SQLite
    drivers log every statement with its bound parameters at DEBUG: session tokens, CSRF
    tokens and hashes would end up in the log of a server started with ``DEBUG``.
    Adapters and repositories log what matters themselves, redacted.
    """
    # Tindeerr logs its own migration summary; Alembic's step-by-step lines are noise.
    logging.getLogger("alembic").setLevel(logging.WARNING)
    for name in ("httpx", "httpcore", "aiosqlite", "sqlalchemy.engine", "sqlalchemy.pool"):
        logging.getLogger(name).setLevel(logging.WARNING)


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
    quiet_noisy_libraries()
    # Requests are logged by the app itself, with the request id and without query strings.
    logging.getLogger("uvicorn.access").disabled = True
    logging.captureWarnings(capture=True)
