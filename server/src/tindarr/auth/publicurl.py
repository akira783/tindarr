"""Checking `public_url` before it is saved (docs/auth.md, section 10).

`public_url` is the address the phones are told to come back to, and it is written into
every pairing QR code. An administrator who mistypes it hands their household's phones
to whatever answers there, so it is never taken on trust:

1. the server draws a nonce and remembers it for a minute;
2. it asks ``GET <public_url>/api/v1/server/info`` for that nonce, through the
   ``PublicUrlProbe`` port (no redirect followed, TLS verified, five seconds);
3. ``GET /server/info`` only answers ``public_url_proof`` when the nonce is one **this
   process** is waiting for, and the proof is
   ``base64url(HMAC-SHA256(tindarr/v1/public-url-proof, nonce))``;
4. the proof is compared in constant time and the nonce is dropped, used or not.

A reverse proxy in front of this same instance passes. Another Tindarr server does not:
it has neither the nonce nor the key. Nothing else can answer the proof, and the nonce
is useless a minute later or to anyone who did not ask for it.

A server that cannot reach its own public address (a router without NAT hairpinning)
cannot pass the check from the console; the operator sets ``TINDARR_PUBLIC_URL``
instead, which is checked once after startup and only logged when it fails.
"""

import hashlib
import hmac
import logging
from base64 import urlsafe_b64encode
from collections import OrderedDict
from datetime import timedelta
from http import HTTPStatus
from typing import Final

from tindarr.auth.events import security_event
from tindarr.auth.tokens import new_token
from tindarr.core.clock import Clock
from tindarr.core.errors import ProblemError
from tindarr.ports.publicurl import ProofFailure, PublicUrlProbe

#: How long a nonce is answered, whatever happens to the request that drew it.
NONCE_LIFETIME: Final = timedelta(seconds=60)
#: Bound on the nonces held at once; the oldest goes first. A minute of checks is far
#: below this, and the per-session limit of ten checks a minute is the real cap.
MAX_PENDING_NONCES: Final = 64

logger = logging.getLogger(__name__)


def public_url_unverified(reason: ProofFailure | str) -> ProblemError:
    """409: the address did not prove it is this server, with a coarse reason."""
    return ProblemError(
        HTTPStatus.CONFLICT,
        "public_url_unverified",
        "That address did not answer as this server; check it and try again.",
        extensions={"reason": reason},
    )


def proof_for(key: bytes, nonce: str) -> str:
    """Return ``base64url(HMAC-SHA256(key, nonce))``, without padding."""
    digest = hmac.new(key, nonce.encode(), hashlib.sha256).digest()
    return urlsafe_b64encode(digest).rstrip(b"=").decode()


class PublicUrlVerifier:
    """Draws the nonces, answers their proof, and runs the check.

    The pending nonces live in memory only: a restart forgets them, and a check started
    before it simply fails and is retried.
    """

    def __init__(self, key: bytes, probe: PublicUrlProbe, clock: Clock) -> None:
        self._key = key
        self._probe = probe
        self._clock = clock
        self._pending: OrderedDict[str, float] = OrderedDict()

    def proof(self, nonce: str | None) -> str | None:
        """Return the proof for a nonce this server is waiting for, else ``None``.

        Read by ``GET /server/info``. An unknown, expired or absent nonce is answered
        with nothing at all, so the field never tells a stranger that the key exists.
        """
        if not nonce:
            return None
        self._drop_expired()
        if nonce not in self._pending:
            return None
        return proof_for(self._key, nonce)

    async def verify(self, public_url: str) -> None:
        """Check that ``public_url`` reaches this server; raise ``public_url_unverified``.

        The nonce is dropped whatever the outcome: one nonce, one check.
        """
        nonce = self._issue()
        try:
            answer = await self._probe.fetch_proof(public_url, nonce)
        finally:
            self._pending.pop(nonce, None)
        if answer.failure is not None:
            self._refuse(answer.failure)
        expected = proof_for(self._key, nonce)
        if answer.proof is None or not hmac.compare_digest(answer.proof, expected):
            self._refuse("proof_mismatch")
        security_event("public_url_verified")

    def _refuse(self, reason: ProofFailure | str) -> None:
        security_event("public_url_unverified", reason=reason)
        raise public_url_unverified(reason)

    def _issue(self) -> str:
        self._drop_expired()
        nonce = new_token()
        self._pending[nonce] = self._clock.monotonic()
        while len(self._pending) > MAX_PENDING_NONCES:
            self._pending.popitem(last=False)
        return nonce

    def _drop_expired(self) -> None:
        deadline = self._clock.monotonic() - NONCE_LIFETIME.total_seconds()
        while self._pending:
            nonce, drawn_at = next(iter(self._pending.items()))
            if drawn_at > deadline:
                return
            del self._pending[nonce]
