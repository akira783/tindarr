"""Authenticated encryption of secrets at rest (AES-256-GCM).

Token format: ``v1:<key id>:<base64url(nonce || ciphertext || tag)>``.

- ``v1`` names the scheme (AES-256-GCM, 96-bit random nonce).
- ``<key id>`` is the fingerprint of the key that encrypted the value, so a future key
  rotation can tell old ciphertexts from new ones.
- The associated data binds the ciphertext to the scheme, the key id and a caller
  ``context`` (for settings, the setting name): a value copied to another setting, or
  re-labelled with another key id, fails to decrypt.
"""

import base64
import binascii
import os
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tindeerr.core.keys import KeyMaterial, KeyPurpose

SCHEME: Final = "v1"
NONCE_SIZE: Final = 12
_TAG_SIZE: Final = 16
_KEY_SIZE: Final = 32


class DecryptionError(Exception):
    """The token is malformed, was encrypted with another key, or was tampered with."""


class SecretCipher:
    """Encrypts and decrypts short secrets with one AES-256-GCM key."""

    def __init__(self, key: bytes, key_id: str) -> None:
        if len(key) != _KEY_SIZE:
            msg = f"AES-256-GCM needs a {_KEY_SIZE}-byte key"
            raise ValueError(msg)
        if not key_id or ":" in key_id:
            msg = "key_id must be non-empty and must not contain ':'"
            raise ValueError(msg)
        self._aead = AESGCM(key)
        self.key_id = key_id

    @classmethod
    def for_settings(cls, keys: KeyMaterial) -> "SecretCipher":
        """Return the cipher for the settings table, keyed by its own HKDF label."""
        return cls(keys.derive(KeyPurpose.SETTINGS_ENCRYPTION), keys.key_id)

    def encrypt(self, plaintext: str, *, context: str) -> str:
        """Encrypt ``plaintext`` with a fresh random nonce, bound to ``context``."""
        nonce = os.urandom(NONCE_SIZE)
        sealed = self._aead.encrypt(nonce, plaintext.encode(), self._aad(self.key_id, context))
        payload = base64.urlsafe_b64encode(nonce + sealed).rstrip(b"=").decode("ascii")
        return f"{SCHEME}:{self.key_id}:{payload}"

    def decrypt(self, token: str, *, context: str) -> str:
        """Decrypt a token produced by ``encrypt`` with the same ``context``."""
        parts = token.split(":")
        if len(parts) != 3 or parts[0] != SCHEME:  # noqa: PLR2004 - scheme:key id:payload
            msg = "unknown encryption scheme"
            raise DecryptionError(msg)
        _, key_id, payload = parts
        if key_id != self.key_id:
            msg = f"encrypted with another key (id {key_id}); was the secret key changed?"
            raise DecryptionError(msg)
        try:
            raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        except (binascii.Error, ValueError):
            msg = "malformed ciphertext"
            raise DecryptionError(msg) from None
        if len(raw) < NONCE_SIZE + _TAG_SIZE:
            msg = "malformed ciphertext"
            raise DecryptionError(msg)
        nonce, sealed = raw[:NONCE_SIZE], raw[NONCE_SIZE:]
        try:
            plaintext = self._aead.decrypt(nonce, sealed, self._aad(key_id, context))
        except InvalidTag:
            msg = "ciphertext failed authentication (tampered, or bound to another context)"
            raise DecryptionError(msg) from None
        return plaintext.decode()

    @staticmethod
    def _aad(key_id: str, context: str) -> bytes:
        return f"tindeerr:{SCHEME}:{key_id}:{context}".encode()
