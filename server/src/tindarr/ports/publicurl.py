"""`public_url` port: fetching this server's own proof through its public address.

Before `public_url` is saved, the server asks the address the phones will use for
``GET /api/v1/server/info`` with a nonce it is waiting for, and expects the matching
proof back (docs/auth.md, section 10). A reverse proxy in front of this same instance
passes; another server does not, and neither does an address that only answers a
redirect somewhere else.

The call itself belongs to an adapter: it must not follow redirects, must verify TLS,
and must give up quickly. What crosses this boundary is only the coarse reason the
contract documents — never a response body, and never the address that answered.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

#: Header carrying the nonce the server is waiting for.
VERIFY_NONCE_HEADER = "Tindarr-Verify-Nonce"
#: Field of ``ServerInfo`` carrying the proof.
PROOF_FIELD = "public_url_proof"

#: Why a check failed, as ``Problem.reason`` of ``public_url_unverified``.
type ProofFailure = Literal["unreachable", "tls_error", "redirected", "unexpected_response"]


@dataclass(frozen=True, slots=True)
class ProofResponse:
    """What the address answered: the proof it returned, or why there is none."""

    proof: str | None = None
    failure: ProofFailure | None = None

    @classmethod
    def failed(cls, failure: ProofFailure) -> "ProofResponse":
        """Build the answer for a check that produced no proof."""
        return cls(failure=failure)


class PublicUrlProbe(Protocol):
    """Fetches ``GET <public_url>/api/v1/server/info`` with a verification nonce."""

    async def fetch_proof(self, public_url: str, nonce: str) -> ProofResponse:
        """Return the proof that address answered with, or the coarse reason it did not.

        Never raises for a remote failure: every outcome is a ``ProofResponse``.
        """
        ...
