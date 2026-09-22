"""The first-run setup code: 60 random bits in a file only the server can read.

docs/auth.md, section 3: the code is 12 Crockford base32 characters written to
``<data>/setup-code`` with mode 0600, and only its SHA-256 is kept in the database. The
logs give the path, never the code.

Reading a code back is forgiving (case, dashes, spaces, and the letters Crockford maps
onto digits), so someone typing what they see in the file gets in.
"""

import hashlib
import hmac
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Final

SETUP_CODE_FILE_NAME: Final = "setup-code"
#: Crockford base32: no I, L, O or U, so a code cannot be misread.
ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
#: 12 characters of 5 bits: 60 bits of entropy.
CODE_LENGTH: Final = 12
_CONFUSED: Final = {"I": "1", "L": "1", "O": "0", "U": "V", "-": "", " ": ""}

logger = logging.getLogger(__name__)


def setup_code_path(data_dir: Path) -> Path:
    """Where the setup code file lives."""
    return data_dir / SETUP_CODE_FILE_NAME


def generate_setup_code() -> str:
    """Return a fresh setup code."""
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def normalize_setup_code(raw: str) -> str:
    """Return what the user typed in canonical form (upper case, Crockford letters)."""
    return "".join(_CONFUSED.get(character, character) for character in raw.strip().upper())


def setup_code_hash(code: str) -> str:
    """Return the SHA-256 of a normalised setup code, as hex."""
    return hashlib.sha256(normalize_setup_code(code).encode()).hexdigest()


def matches(code: str, stored_hash: str) -> bool:
    """Compare a typed code with the stored hash, in constant time."""
    return hmac.compare_digest(setup_code_hash(code), stored_hash)


def write_setup_code(path: Path, code: str) -> None:
    """Write the code to ``path`` with mode 0600, replacing any stale file.

    The file is created with ``O_EXCL`` after the old one is unlinked, so it never
    exists with wider permissions, and a symbolic link planted there is refused.
    """
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(code + "\n")
        file.flush()
        os.fsync(file.fileno())


def read_setup_code(path: Path) -> str | None:
    """Return the code in ``path``, or ``None`` when there is no usable file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        with os.fdopen(fd, encoding="utf-8") as file:
            content = file.read(1024)
    except OSError:  # pragma: no cover - the descriptor was just opened
        return None
    code = normalize_setup_code(content)
    return code or None


def remove_setup_code(path: Path) -> None:
    """Delete the setup code file, if it is still there."""
    path.unlink(missing_ok=True)
