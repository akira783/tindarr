"""Access tokens and opaque tokens (docs/auth.md, section 7)."""

from datetime import timedelta
from typing import Any

import jwt
import pytest

from tests.support import FakeClock
from tindeerr.auth.tokens import (
    ACCESS_TOKEN_LIFETIME,
    AUDIENCE,
    LEEWAY,
    AccessTokens,
    new_token,
    token_hash,
    tokens_equal,
)
from tindeerr.core.errors import ProblemError
from tindeerr.core.keys import KeyMaterial, KeyPurpose


def signing_key(keys: KeyMaterial) -> bytes:
    return keys.derive(KeyPurpose.JWT_SIGNING)


def claims_of(token: str, keys: KeyMaterial) -> dict[str, Any]:
    return jwt.decode(
        token,
        signing_key(keys),
        algorithms=["HS256"],
        audience=AUDIENCE,
        issuer="tindeerr:install-1",
        options={"verify_exp": False},  # the fake clock is not the system clock
    )


def test_opaque_tokens_are_random_and_hashed() -> None:
    first, second = new_token(), new_token()
    assert first != second
    assert len(first) >= 43  # 256 bits in base64url
    assert token_hash(first) == token_hash(first)
    assert token_hash(first) != token_hash(second)
    assert len(token_hash(first)) == 64
    assert tokens_equal(first, first)
    assert not tokens_equal(first, second)


def test_an_issued_token_carries_the_required_header_and_claims(
    access_tokens: AccessTokens, keys: KeyMaterial, clock: FakeClock
) -> None:
    issued = access_tokens.issue("user-1", "session-1")

    header: dict[str, Any] = jwt.get_unverified_header(issued.value)
    claims = claims_of(issued.value, keys)

    assert header == {"alg": "HS256", "typ": "at+jwt", "kid": keys.key_id}
    assert claims["iss"] == "tindeerr:install-1"
    assert claims["aud"] == AUDIENCE
    assert (claims["sub"], claims["sid"]) == ("user-1", "session-1")
    assert claims["exp"] - claims["iat"] == ACCESS_TOKEN_LIFETIME.total_seconds()
    assert claims["jti"]
    assert "role" not in claims  # docs/adr/0010: authorization reads the database
    assert issued.expires_at == clock.now() + ACCESS_TOKEN_LIFETIME


def test_a_fresh_token_verifies(access_tokens: AccessTokens) -> None:
    issued = access_tokens.issue("user-1", "session-1")
    verified = access_tokens.verify(issued.value)
    assert (verified.user_id, verified.session_id) == ("user-1", "session-1")


def test_each_token_is_unique(access_tokens: AccessTokens) -> None:
    first = access_tokens.issue("user-1", "session-1")
    second = access_tokens.issue("user-1", "session-1")
    assert first.value != second.value


def test_an_expired_token_says_so_and_only_after_the_leeway(
    access_tokens: AccessTokens, clock: FakeClock
) -> None:
    issued = access_tokens.issue("user-1", "session-1")
    clock.advance(ACCESS_TOKEN_LIFETIME)
    access_tokens.verify(issued.value)  # still inside the leeway
    clock.advance(LEEWAY)
    with pytest.raises(ProblemError) as caught:
        access_tokens.verify(issued.value)
    assert (caught.value.status, caught.value.code) == (401, "token_expired")


def test_a_token_from_the_future_is_refused(access_tokens: AccessTokens, clock: FakeClock) -> None:
    issued = access_tokens.issue("user-1", "session-1")
    clock.advance(-LEEWAY.total_seconds() * 4)
    with pytest.raises(ProblemError, match="Invalid access token"):
        access_tokens.verify(issued.value)


def forged(keys: KeyMaterial, **changes: Any) -> str:
    """Sign a token with the real key but the wrong claims or header."""
    payload: dict[str, Any] = {
        "iss": "tindeerr:install-1",
        "aud": AUDIENCE,
        "sub": "user-1",
        "sid": "session-1",
        "iat": 1790000000,
        "exp": 1790000900,
        "jti": "j1",
    }
    header: dict[str, Any] = {"typ": "at+jwt", "kid": keys.key_id}
    payload.update(changes.pop("payload", {}))
    header.update(changes.pop("header", {}))
    header = {name: value for name, value in header.items() if value is not None}
    for name in changes.pop("without", ()):
        payload.pop(name, None)
    return jwt.encode(
        payload, signing_key(keys), algorithm=changes.pop("algorithm", "HS256"), headers=header
    )


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"payload": {"aud": "someone-else"}}, id="wrong audience"),
        pytest.param({"payload": {"iss": "tindeerr:another-install"}}, id="wrong issuer"),
        pytest.param({"header": {"typ": "JWT"}}, id="wrong type"),
        pytest.param({"header": {"typ": None}}, id="no type"),
        pytest.param({"header": {"kid": "0badc0de"}}, id="wrong key id"),
        pytest.param({"header": {"kid": None}}, id="no key id"),
        pytest.param({"without": ["sub"]}, id="no subject"),
        pytest.param({"without": ["sid"]}, id="no session"),
        pytest.param({"without": ["iat"]}, id="no issued at"),
        pytest.param({"without": ["exp"]}, id="no expiry"),
        pytest.param({"without": ["iss"]}, id="no issuer"),
        pytest.param({"without": ["aud"]}, id="no audience"),
    ],
)
def test_tokens_with_the_wrong_header_or_claims_are_refused(
    access_tokens: AccessTokens, keys: KeyMaterial, clock: FakeClock, changes: Any
) -> None:
    token = forged(keys, **changes)
    clock.advance(1790000100 - clock.now().timestamp())
    with pytest.raises(ProblemError) as caught:
        access_tokens.verify(token)
    assert (caught.value.status, caught.value.code) == (401, "unauthorized")


def test_a_token_signed_with_another_key_is_refused(
    access_tokens: AccessTokens, keys: KeyMaterial, clock: FakeClock
) -> None:
    other = KeyMaterial(b"another-master-key-0123456789abcd", "environment")
    token = jwt.encode(
        {
            "iss": "tindeerr:install-1",
            "aud": AUDIENCE,
            "sub": "user-1",
            "sid": "session-1",
            "iat": int(clock.now().timestamp()),
            "exp": int(clock.now().timestamp()) + 900,
        },
        other.derive(KeyPurpose.JWT_SIGNING),
        algorithm="HS256",
        headers={"typ": "at+jwt", "kid": keys.key_id},
    )
    with pytest.raises(ProblemError, match="Invalid access token"):
        access_tokens.verify(token)


def test_an_unsigned_token_is_refused(access_tokens: AccessTokens, clock: FakeClock) -> None:
    unsigned = jwt.encode(
        {
            "iss": "tindeerr:install-1",
            "aud": AUDIENCE,
            "sub": "user-1",
            "sid": "session-1",
            "iat": int(clock.now().timestamp()),
            "exp": int(clock.now().timestamp()) + 900,
        },
        key="",
        algorithm="none",
        headers={"typ": "at+jwt"},
    )
    with pytest.raises(ProblemError, match="Invalid access token"):
        access_tokens.verify(unsigned)


@pytest.mark.parametrize("token", ["", "not-a-token", "a.b.c", "a.b", "..", "x" * 500])
def test_garbage_is_refused(access_tokens: AccessTokens, token: str) -> None:
    with pytest.raises(ProblemError, match="Invalid access token"):
        access_tokens.verify(token)


def test_changing_the_master_key_invalidates_every_token(
    access_tokens: AccessTokens, clock: FakeClock
) -> None:
    issued = access_tokens.issue("user-1", "session-1")
    other = KeyMaterial(b"a-completely-different-master-key", "environment")
    rotated = AccessTokens(other.derive(KeyPurpose.JWT_SIGNING), other.key_id, "install-1", clock)
    with pytest.raises(ProblemError, match="Invalid access token"):
        rotated.verify(issued.value)
    assert rotated.verify(rotated.issue("user-1", "session-1").value).user_id == "user-1"


def test_the_leeway_is_at_most_thirty_seconds() -> None:
    assert timedelta(seconds=30) >= LEEWAY
