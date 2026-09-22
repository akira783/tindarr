"""Opaque tokens and the access-token JWT (docs/auth.md, section 7).

Opaque tokens (session cookies, refresh tokens, CSRF tokens) are 256 random bits in
base64url; only their SHA-256 is stored, so a database copy does not hand over sessions.

Access tokens are JWTs signed with HS256 and the HKDF sub-key
``tindarr/v1/jwt-signing``. Their expiry is checked against the injected clock rather
than PyJWT's own (which reads the system clock), so lifetimes are testable; everything
else — signature, algorithm, required claims, issuer and audience — is PyJWT's work.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import jwt

from tindarr.auth.errors import token_expired, unauthorized
from tindarr.core.clock import Clock

#: 256 bits, like the session cookie and refresh tokens.
TOKEN_BYTES: Final = 32
ALGORITHM: Final = "HS256"
TOKEN_TYPE: Final = "at+jwt"  # noqa: S105 - a JWT header value, not a credential
AUDIENCE: Final = "tindarr-api"
ISSUER_PREFIX: Final = "tindarr:"
ACCESS_TOKEN_LIFETIME: Final = timedelta(minutes=15)
#: Clock skew allowed on ``exp`` and ``iat``.
LEEWAY: Final = timedelta(seconds=30)
_REQUIRED_CLAIMS: Final = ["iss", "aud", "sub", "sid", "iat", "exp"]


def new_token() -> str:
    """Return a fresh 256-bit opaque token, base64url without padding."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    """Return the SHA-256 of a token, as hex: what is stored and compared."""
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_equal(left: str, right: str) -> bool:
    """Compare two tokens in constant time.

    The values are compared as bytes: one of them usually comes from a header, where a
    non-ASCII character would make ``hmac.compare_digest`` raise on strings.
    """
    return hmac.compare_digest(left.encode("utf-8", "surrogateescape"), right.encode())


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A signed access token and when it expires."""

    value: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AccessClaims:
    """What a verified access token says."""

    user_id: str
    session_id: str


class AccessTokens:
    """Issues and verifies access tokens for one install and one signing key."""

    def __init__(self, key: bytes, key_id: str, install_id: str, clock: Clock) -> None:
        self._key = key
        self._key_id = key_id
        self._issuer = f"{ISSUER_PREFIX}{install_id}"
        self._clock = clock

    def issue(self, user_id: str, session_id: str) -> AccessToken:
        """Return a token for this user and session, valid 15 minutes."""
        issued_at = self._clock.now()
        expires_at = issued_at + ACCESS_TOKEN_LIFETIME
        claims: dict[str, object] = {
            "iss": self._issuer,
            "aud": AUDIENCE,
            "sub": user_id,
            "sid": session_id,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": secrets.token_urlsafe(16),
        }
        value = jwt.encode(
            claims,
            self._key,
            algorithm=ALGORITHM,
            headers={"typ": TOKEN_TYPE, "kid": self._key_id},
        )
        return AccessToken(value, expires_at)

    def verify(self, token: str) -> AccessClaims:
        """Check a token and return its claims.

        Raises ``token_expired`` past ``exp`` (plus the leeway) and ``unauthorized`` for
        anything else: another algorithm, type, key id, issuer or audience, a missing
        claim, a bad signature or a token issued in the future.
        """
        self._check_header(token)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._key,
                algorithms=[ALGORITHM],
                audience=AUDIENCE,
                issuer=self._issuer,
                # The clock is ours, so exp and iat are checked below.
                options={"require": _REQUIRED_CLAIMS, "verify_exp": False, "verify_iat": False},
            )
        except jwt.InvalidTokenError:
            raise unauthorized("Invalid access token.") from None
        self._check_times(claims)
        subject, session_id = claims.get("sub"), claims.get("sid")
        if not isinstance(subject, str) or not isinstance(session_id, str):
            raise unauthorized("Invalid access token.")
        return AccessClaims(subject, session_id)

    def _check_header(self, token: str) -> None:
        try:
            header: dict[str, Any] = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            raise unauthorized("Invalid access token.") from None
        if header.get("alg") != ALGORITHM or header.get("typ") != TOKEN_TYPE:
            raise unauthorized("Invalid access token.")
        if not tokens_equal(str(header.get("kid", "")), self._key_id):
            # Another key signed it: the master key was changed, or it is not ours.
            raise unauthorized("Invalid access token.")

    def _check_times(self, claims: dict[str, Any]) -> None:
        now = self._clock.now().timestamp()
        expires_at, issued_at = claims.get("exp"), claims.get("iat")
        if not isinstance(expires_at, int | float) or not isinstance(issued_at, int | float):
            raise unauthorized("Invalid access token.")
        if issued_at > now + LEEWAY.total_seconds():
            raise unauthorized("Invalid access token.")
        if expires_at + LEEWAY.total_seconds() <= now:
            raise token_expired()
