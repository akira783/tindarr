import base64
import os

import pytest

from tindarr.core.crypto import DecryptionError, SecretCipher
from tindarr.core.keys import KeyMaterial, KeyPurpose

KEY = os.urandom(32)


@pytest.fixture
def cipher() -> SecretCipher:
    return SecretCipher(KEY, "0a1b2c3d")


def test_round_trip(cipher: SecretCipher) -> None:
    token = cipher.encrypt("sk-live-123456", context="setting:api_key")
    assert token.startswith("v1:0a1b2c3d:")
    assert "sk-live" not in token
    assert cipher.decrypt(token, context="setting:api_key") == "sk-live-123456"


def test_unicode_and_empty_values(cipher: SecretCipher) -> None:
    for value in ("", "mot de passe é ✓"):
        assert cipher.decrypt(cipher.encrypt(value, context="c"), context="c") == value


def test_each_encryption_uses_a_fresh_nonce(cipher: SecretCipher) -> None:
    assert cipher.encrypt("same", context="c") != cipher.encrypt("same", context="c")


def test_tampered_ciphertext_is_rejected(cipher: SecretCipher) -> None:
    scheme, key_id, payload = cipher.encrypt("secret-value", context="c").split(":")
    raw = bytearray(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    raw[-1] ^= 0x01
    tampered = base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()
    with pytest.raises(DecryptionError, match="authentication"):
        cipher.decrypt(f"{scheme}:{key_id}:{tampered}", context="c")


def test_ciphertext_is_bound_to_its_context(cipher: SecretCipher) -> None:
    token = cipher.encrypt("secret-value", context="setting:media_server_api_key")
    with pytest.raises(DecryptionError, match="authentication"):
        cipher.decrypt(token, context="setting:llm_api_key")


def test_ciphertext_is_bound_to_its_key_id() -> None:
    original = SecretCipher(KEY, "0a1b2c3d")
    relabelled = SecretCipher(KEY, "ffffffff")
    token = original.encrypt("secret-value", context="c").replace("0a1b2c3d", "ffffffff", 1)
    with pytest.raises(DecryptionError, match="authentication"):
        relabelled.decrypt(token, context="c")


def test_other_key_is_reported(cipher: SecretCipher) -> None:
    other = SecretCipher(os.urandom(32), "99999999")
    with pytest.raises(DecryptionError, match="another key"):
        other.decrypt(cipher.encrypt("x", context="c"), context="c")


@pytest.mark.parametrize(
    "token",
    ["", "garbage", "v2:0a1b2c3d:AAAA", "v1:0a1b2c3d", "v1:0a1b2c3d:!!!!", "v1:0a1b2c3d:AAAA"],
)
def test_malformed_tokens_are_rejected(cipher: SecretCipher, token: str) -> None:
    with pytest.raises(DecryptionError):
        cipher.decrypt(token, context="c")


def test_invalid_construction() -> None:
    with pytest.raises(ValueError, match="32-byte"):
        SecretCipher(b"short", "id")
    with pytest.raises(ValueError, match="key_id"):
        SecretCipher(KEY, "a:b")


def test_settings_cipher_uses_its_own_sub_key() -> None:
    keys = KeyMaterial(b"m" * 32, "environment")
    cipher = SecretCipher.for_settings(keys)
    assert cipher.key_id == keys.key_id
    token = cipher.encrypt("v", context="c")
    same_key = SecretCipher(keys.derive(KeyPurpose.SETTINGS_ENCRYPTION), keys.key_id)
    jwt_key = SecretCipher(keys.derive(KeyPurpose.JWT_SIGNING), keys.key_id)
    assert same_key.decrypt(token, context="c") == "v"
    with pytest.raises(DecryptionError):
        jwt_key.decrypt(token, context="c")
