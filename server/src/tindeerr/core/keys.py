"""Master key material and the sub-keys derived from it.

The master key comes from ``TINDEERR_SECRET_KEY`` (or ``_FILE``). Without it, a random
key is generated once into ``<data_dir>/secret.key`` with mode 0600. Either way, the
key is only used as HKDF input: every purpose gets its own sub-key through a distinct
label, so no two features ever share a key.

Surrounding whitespace is ignored whatever the source, so the same key given through
the environment, a Docker secret or the key file derives the same sub-keys.

The key file is written to a temporary file, flushed, then hard-linked into place: it
never exists half-written, and ``os.link`` never replaces a file. Two processes starting
at once on a fresh data directory therefore agree on one key: the loser of the race
reads the winner's file.
"""

import errno
import os
import secrets
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_FILE_NAME = "secret.key"
MIN_SECRET_KEY_LENGTH = 32
_GENERATED_KEY_BYTES = 32

type KeySource = Literal["environment", "file", "generated"]


class KeyPurpose(StrEnum):
    """HKDF ``info`` labels. Changing one invalidates everything derived from it."""

    SETTINGS_ENCRYPTION = "tindeerr/v1/settings-encryption"
    JWT_SIGNING = "tindeerr/v1/jwt-signing"  # reserved for step 2 (access tokens)
    KEY_ID = "tindeerr/v1/key-id"


class SecretKeyError(Exception):
    """The master key is missing, too short or unsafely stored."""


@dataclass(frozen=True)
class KeyMaterial:
    """Master key bytes. Never printed: ``repr`` only shows where the key came from."""

    secret: bytes = field(repr=False)
    source: KeySource

    def derive(self, purpose: KeyPurpose, length: int = 32) -> bytes:
        """Derive the sub-key for ``purpose`` (HKDF-SHA256, no salt, label as info)."""
        hkdf = HKDF(algorithm=SHA256(), length=length, salt=None, info=purpose.value.encode())
        return hkdf.derive(self.secret)

    @property
    def key_id(self) -> str:
        """Short public fingerprint of the key, stored next to ciphertexts."""
        return self.derive(KeyPurpose.KEY_ID, 4).hex()


def _normalize(value: str, origin: str) -> bytes:
    value = value.strip()
    if len(value) < MIN_SECRET_KEY_LENGTH:
        msg = f"{origin} must be at least {MIN_SECRET_KEY_LENGTH} characters long"
        raise SecretKeyError(msg)
    return value.encode()


def load_key_material(secret_key: str | None, data_dir: Path) -> KeyMaterial:
    """Return the master key from ``secret_key``, the key file, or a newly generated file.

    An existing key file must be a regular file owned by the user running the server and
    not accessible by group or others, like OpenSSH requires for private keys; otherwise
    it is refused rather than silently used.
    """
    if secret_key is not None:
        return KeyMaterial(_normalize(secret_key, "TINDEERR_SECRET_KEY"), "environment")

    path = data_dir / KEY_FILE_NAME
    try:
        return _read_key_file(path)
    except FileNotFoundError:
        return _create_key_file(path)


def _read_key_file(path: Path) -> KeyMaterial:
    # O_NOFOLLOW: never read through a symlink. O_NONBLOCK: a FIFO planted there cannot
    # hang the startup (it is refused below as not a regular file).
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise
        msg = f"{path} is a symbolic link; put the key file itself there"
        raise SecretKeyError(msg) from None
    try:
        _check_key_file(path, os.fstat(fd))
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, encoding="utf-8") as file:
        value = file.read()
    if not value.strip():
        # Only an interrupted write by an older version, or a manual edit, leaves this.
        # Nothing can have been encrypted with an empty key, but replacing a file the
        # operator may have created is their call.
        msg = f"{path} is empty; delete it to have a new key generated, or put the key in it"
        raise SecretKeyError(msg)
    return KeyMaterial(_normalize(value, str(path)), "file")


def _check_key_file(path: Path, info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        msg = f"{path} is not a regular file"
        raise SecretKeyError(msg)
    if info.st_uid != os.geteuid():
        msg = (
            f"{path} is owned by uid {info.st_uid}, but the server runs as uid "
            f"{os.geteuid()}; fix its owner and restart"
        )
        raise SecretKeyError(msg)
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        msg = (
            f"{path} is accessible by group or others (mode {stat.filemode(info.st_mode)}); "
            f"run 'chmod 600 {path}' and restart"
        )
        raise SecretKeyError(msg)


def _create_key_file(path: Path) -> KeyMaterial:
    value = secrets.token_urlsafe(_GENERATED_KEY_BYTES)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(value + "\n")
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            # Another process created the key meanwhile: use theirs.
            return _read_key_file(path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return KeyMaterial(value.encode(), "generated")


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
