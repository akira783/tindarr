"""Checking `public_url` before it is saved (docs/auth.md, section 10).

`public_url` is the address the phones are told to come back to, and it is written into
every pairing QR code. An administrator who mistypes it hands their household's phones
to whatever answers there, so it is never taken on trust:

1. the server draws a nonce and remembers it for a minute;
2. it asks ``GET <public_url>/api/v1/server/info`` for that nonce, through the
   ``PublicUrlProbe`` port (no redirect followed, TLS verified, five seconds);
3. ``GET /server/info`` answers ``public_url_proof`` only when the nonce is one **this
   process** is waiting for, and the proof is
   ``base64url(HMAC-SHA256(key, nonce + "|" + <the host the request arrived at>))``;
4. the proof is compared in constant time against the one the candidate host should
   have produced, and the nonce is dropped whatever happened.

**Why the proof is bound to the host.** Any address that can *reach* this server could
otherwise relay: it would take the nonce out of the probe, ask this server for the proof
over its own connection, and echo it back. Binding the proof to the ``Host`` the request
arrived at closes that: a relay would have to present the candidate host, and a host
this server does not answer to is refused before routing (docs/auth.md, section 1). It
follows that the candidate host must already be an allowed host — the console is
normally open at it — which is why ``PATCH /admin/settings`` refuses an unknown host up
front instead of calling it.

A reverse proxy in front of this same instance passes, because the request reaches it
with that very host. A different server does not: it has neither the key nor a way to
be asked for a proof under that host.

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
from tindarr.core.errors import ProblemError, RateLimitedError
from tindarr.ports.publicurl import ProofFailure, PublicUrlProbe

#: How long a nonce is answered, whatever happens to the request that drew it.
NONCE_LIFETIME: Final = timedelta(seconds=60)
#: Bound on the nonces held at once. The per-session limit of ten checks a minute is
#: the real cap; past this one, a new check is refused rather than silently dropping a
#: nonce another check is still waiting on.
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


def proof_for(key: bytes, nonce: str, host: str) -> str:
    """Return the proof for one nonce **at one host**, base64url without padding.

    The host is part of the message, not of the key: a proof handed to a caller that
    reached this server under another name is worthless to whoever is checking the
    candidate one.
    """
    message = f"{nonce}|{host.lower()}".encode()
    digest = hmac.new(key, message, hashlib.sha256).digest()
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

    def proof(self, nonce: str | None, host: str | None) -> str | None:
        """Return the proof for a nonce this server is waiting for, else ``None``.

        Read by ``GET /server/info`` with the host that request arrived at. An unknown,
        expired or absent nonce is answered with nothing at all, so the field never
        tells a stranger that the key exists; a nonce is answered **once**, so a second
        caller holding it gets nothing either.
        """
        if not nonce or host is None:
            return None
        self._drop_expired()
        if self._pending.pop(nonce, None) is None:
            return None
        return proof_for(self._key, nonce, host)

    async def verify(self, public_url: str, host: str) -> None:
        """Check that ``public_url`` reaches this server; raise ``public_url_unverified``.

        ``host`` is the host of ``public_url``: the proof must be the one this server
        produces for a request that arrived under that name. The nonce is dropped
        whatever the outcome — one nonce, one check.
        """
        nonce = self._issue()
        try:
            answer = await self._probe.fetch_proof(public_url, nonce)
        finally:
            self._pending.pop(nonce, None)
        if answer.failure is not None:
            self._refuse(answer.failure)
        expected = proof_for(self._key, nonce, host)
        if answer.proof is None or not hmac.compare_digest(answer.proof, expected):
            self._refuse("proof_mismatch")
        security_event("public_url_verified")

    def _refuse(self, reason: ProofFailure | str) -> None:
        security_event("public_url_unverified", reason=reason)
        raise public_url_unverified(reason)

    def _issue(self) -> str:
        self._drop_expired()
        if len(self._pending) >= MAX_PENDING_NONCES:
            # Never drop a nonce another check is still waiting on: refuse this one.
            raise RateLimitedError(
                int(NONCE_LIFETIME.total_seconds() * 1000),
                "Too many public address checks at once; try again in a minute.",
            )
        nonce = new_token()
        self._pending[nonce] = self._clock.monotonic()
        return nonce

    def _drop_expired(self) -> None:
        deadline = self._clock.monotonic() - NONCE_LIFETIME.total_seconds()
        while self._pending:
            nonce, drawn_at = next(iter(self._pending.items()))
            if drawn_at > deadline:
                return
            del self._pending[nonce]
