"""Master key material and the sub-keys derived from it.

The master key comes from ``TINDEERR_SECRET_KEY`` (or ``_FILE``). Without it, a random
key is generated once into ``<data_dir>/secret.key`` with mode 0600. Either way, the
key is only used as HKDF input: every purpose gets its own sub-key through a distinct
label, so no two features ever share a key.
"""

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


def _check_length(value: str, origin: str) -> bytes:
    if len(value) < MIN_SECRET_KEY_LENGTH:
        msg = f"{origin} must be at least {MIN_SECRET_KEY_LENGTH} characters long"
        raise SecretKeyError(msg)
    return value.encode()


def load_key_material(secret_key: str | None, data_dir: Path) -> KeyMaterial:
    """Return the master key from ``secret_key``, the key file, or a newly generated file.

    An existing key file readable or writable by group or others is refused, like
    OpenSSH does with private keys, rather than silently used.
    """
    if secret_key is not None:
        return KeyMaterial(_check_length(secret_key, "TINDEERR_SECRET_KEY"), "environment")

    path = data_dir / KEY_FILE_NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return _generate(path)
    with os.fdopen(fd, encoding="utf-8") as file:
        mode = os.fstat(file.fileno()).st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            msg = (
                f"{path} is accessible by group or others (mode {stat.filemode(mode)}); "
                f"run 'chmod 600 {path}' and restart"
            )
            raise SecretKeyError(msg)
        value = file.read().strip()
    return KeyMaterial(_check_length(value, str(path)), "file")


def _generate(path: Path) -> KeyMaterial:
    value = secrets.token_urlsafe(_GENERATED_KEY_BYTES)
    # O_EXCL: never overwrite a key that appeared meanwhile, never follow a symlink.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(value + "\n")
        file.flush()
        os.fsync(file.fileno())
    return KeyMaterial(value.encode(), "generated")
