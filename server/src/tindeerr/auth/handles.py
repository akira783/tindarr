"""Sign-in handles: what a client holds instead of a plex.tv PIN or a Jellyfin secret.

docs/auth.md, section 5. A client never sees the upstream credential. It gets a
**handle** — 128 random bits — that this registry can turn back into the PIN or the
secret, and only for the client it was created for.

Three things make a handle usable, and all three are checked on every call:

- its **purpose** (``sign_in``, ``reauth``, ``owner_token``): a handle created to sign
  in cannot be used to prove a step-up, nor to hand a Plex owner token to the connector;
- its **initiator binding**: the app proves it with a PKCE ``code_verifier``, the
  console with its pre-auth cookie, and a ``reauth`` or ``owner_token`` handle with the
  session that created it. Comparisons are constant time;
- its **expiry and single use**. Anything wrong — unknown, expired, consumed, another
  purpose, another browser — gets the same ``410``, so a handle tells nothing about
  handles that are not yours.

Handles live **in memory only**, so a restart forgets them and clients simply start
again. Two caps bound that memory: five outstanding per client address and two hundred
in all. The global one is the only limit in Tindeerr that refuses rather than slows
down, because it protects a fixed resource (docs/auth.md, section 8).
"""

import base64
import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal

from tindeerr.auth import errors
from tindeerr.auth.events import security_event
from tindeerr.auth.ratelimit import SlidingWindow
from tindeerr.auth.tokens import token_hash, tokens_equal
from tindeerr.core.clock import Clock
from tindeerr.core.errors import PendingError, ProblemError, RateLimitedError
from tindeerr.ports.plextv import PlexPin
from tindeerr.storage.ids import new_id

type HandleKind = Literal["plex_pin", "quick_connect"]
type HandlePurpose = Literal["sign_in", "reauth", "owner_token"]
type BindingKind = Literal["pkce", "preauth", "session"]

#: How long a handle may live, whatever the upstream expiry says.
#: Plex PINs live 15 minutes upstream; Quick Connect codes 10, and the cap is shorter so
#: the cleanup sweep still has time to collect an approval nobody came back for.
MAX_LIFETIME: Final[dict[str, timedelta]] = {
    "plex_pin": timedelta(minutes=10),
    "quick_connect": timedelta(minutes=5),
}
#: One upstream call per second per handle; a faster caller is told to wait.
POLL_INTERVAL_S: Final = 1.0
POLL_RETRY_MS: Final = 1000
#: Outstanding handles allowed per client address, and in all.
MAX_PER_CLIENT: Final = 5
MAX_TOTAL: Final = 200


def pkce_challenge(code_verifier: str) -> str:
    """Return ``base64url(SHA-256(code_verifier))`` without padding (PKCE S256)."""
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


@dataclass(frozen=True, slots=True)
class Binding:
    """Who may use a handle, as a value that can be rebuilt from the proof presented."""

    kind: BindingKind
    value: str

    @staticmethod
    def pkce(code_challenge: str) -> "Binding":
        """Bind to a PKCE S256 challenge (the app keeps the verifier)."""
        return Binding("pkce", code_challenge)

    @staticmethod
    def verifier(code_verifier: str) -> "Binding":
        """Return the binding a ``code_verifier`` proves."""
        return Binding("pkce", pkce_challenge(code_verifier))

    @staticmethod
    def cookie(value: str) -> "Binding":
        """Bind to the console's pre-auth cookie; only its hash is kept."""
        return Binding("preauth", token_hash(value))

    @staticmethod
    def session(session_id: str) -> "Binding":
        """Bind to the session that created the handle (``reauth``, ``owner_token``)."""
        return Binding("session", session_id)

    def matches(self, other: "Binding") -> bool:
        """Whether the proof presented rebuilds this binding."""
        return self.kind == other.kind and tokens_equal(self.value, other.value)


@dataclass
class Handle:
    """One outstanding sign-in handle and everything known about it."""

    id: str
    kind: HandleKind
    purpose: HandlePurpose
    binding: Binding
    client_key: str
    expires_at: datetime
    #: The upstream credential: a plex.tv PIN, or a Jellyfin Quick Connect secret.
    pin: PlexPin | None = None
    secret: str | None = None
    #: The owner token an ``owner_token`` PIN collected, and the account that approved it.
    token: str | None = None
    account_name: str | None = None
    consumed: bool = False
    #: Set once a Quick Connect approval was collected, so the sweep leaves it alone.
    collected: bool = False
    _last_poll: float | None = field(default=None, repr=False)

    def expired(self, now: datetime) -> bool:
        """Whether the handle has run out of time."""
        return now >= self.expires_at

    def throttle(self, monotonic: float) -> None:
        """Allow one upstream call per second; a faster caller gets ``202`` instead."""
        if self._last_poll is not None and monotonic - self._last_poll < POLL_INTERVAL_S:
            raise PendingError(POLL_RETRY_MS)
        self._last_poll = monotonic


class HandleRegistry:
    """The process-wide registry of outstanding handles."""

    def __init__(
        self,
        clock: Clock,
        creation_limit: SlidingWindow,
        max_per_client: int = MAX_PER_CLIENT,
        max_total: int = MAX_TOTAL,
    ) -> None:
        self._clock = clock
        self._creation_limit = creation_limit
        self._max_per_client = max_per_client
        self._max_total = max_total
        self._handles: OrderedDict[str, Handle] = OrderedDict()

    # --- creating -------------------------------------------------------------------

    def create(  # noqa: PLR0913 - one per part of a handle, all keyword but the first
        self,
        kind: HandleKind,
        purpose: HandlePurpose,
        binding: Binding,
        *,
        client_key: str,
        upstream_expiry: datetime | None = None,
        pin: PlexPin | None = None,
        secret: str | None = None,
    ) -> Handle:
        """Register a handle, after the creation limits and the two caps.

        ``upstream_expiry`` is what the media server or plex.tv said; the handle never
        outlives ``MAX_LIFETIME`` whatever that was.
        """
        now = self._clock.now()
        self._forget_expired(now)
        self._creation_limit.hit(client_key)
        self._check_caps(client_key)
        handle = Handle(
            id=new_id(),
            kind=kind,
            purpose=purpose,
            binding=binding,
            client_key=client_key,
            expires_at=self._expiry(kind, upstream_expiry, now),
            pin=pin,
            secret=secret,
        )
        self._handles[handle.id] = handle
        security_event("handle_created", handle_kind=kind, purpose=purpose)
        return handle

    @staticmethod
    def _expiry(kind: HandleKind, upstream: datetime | None, now: datetime) -> datetime:
        capped = now + MAX_LIFETIME[kind]
        return min(upstream, capped) if upstream is not None else capped

    def _check_caps(self, client_key: str) -> None:
        if len(self._handles) >= self._max_total:
            raise self._too_many("global")
        mine = sum(1 for handle in self._handles.values() if handle.client_key == client_key)
        if mine >= self._max_per_client:
            raise self._too_many("per_client")

    @staticmethod
    def _too_many(scope: str) -> RateLimitedError:
        security_event("handle_cap_reached", scope=scope)
        return RateLimitedError(
            POLL_RETRY_MS, "Too many sign-ins are waiting; finish or abandon one first."
        )

    # --- using ----------------------------------------------------------------------

    def use(
        self, handle_id: str, kind: HandleKind, purpose: HandlePurpose, binding: Binding
    ) -> Handle:
        """Return the live handle matching all four values, or raise its ``410``.

        Unknown, expired, consumed, another kind, another purpose and another initiator
        are deliberately indistinguishable.
        """
        now = self._clock.now()
        self._forget_expired(now)
        handle = self._handles.get(handle_id)
        if (
            handle is None
            or handle.kind != kind
            or handle.consumed
            or handle.expired(now)
            or handle.purpose != purpose
            or not handle.binding.matches(binding)
        ):
            raise gone(kind)
        return handle

    def throttle(self, handle: Handle) -> None:
        """Allow one upstream call per second for this handle; otherwise ``202``."""
        handle.throttle(self._clock.monotonic())

    def spend(self, handle: Handle) -> None:
        """Mark a handle used and drop it: it can never serve a second sign-in."""
        handle.consumed = True
        self._handles.pop(handle.id, None)

    def drop(self, handle_id: str) -> None:
        """Forget a handle (a flow that failed in a way that makes it useless)."""
        self._handles.pop(handle_id, None)

    # --- the owner-token handles the connector reads --------------------------------

    async def peek(self, handle: str, session_id: str) -> str:
        """Return an approved owner token without consuming its handle.

        A failed connection test must leave the handle usable, so the administrator can
        fix the URL and try again (docs/auth.md, section 5).
        """
        entry = self.use(handle, "plex_pin", "owner_token", Binding.session(session_id))
        if entry.token is None:
            raise errors.plex_pin_pending()
        return entry.token

    async def consume(self, handle: str, session_id: str) -> None:
        """Mark an owner-token handle used, once its token has been stored."""
        self.spend(self.use(handle, "plex_pin", "owner_token", Binding.session(session_id)))

    # --- housekeeping ---------------------------------------------------------------

    def sweep(self) -> list[Handle]:
        """Drop every expired handle and return those that may still hold a session.

        A Quick Connect request the user approved created a session on Jellyfin the
        moment they approved it, even if nobody ever collected it. Those are handed to
        the caller, which collects them once so the session can be closed.
        """
        now = self._clock.now()
        expired = [handle for handle in self._handles.values() if handle.expired(now)]
        for handle in expired:
            del self._handles[handle.id]
        return [
            handle
            for handle in expired
            if handle.kind == "quick_connect" and not handle.collected and not handle.consumed
        ]

    def _forget_expired(self, now: datetime) -> None:
        for handle_id, handle in list(self._handles.items()):
            if handle.expired(now):
                del self._handles[handle_id]

    @property
    def outstanding(self) -> int:
        """How many handles are registered right now."""
        return len(self._handles)


def gone(kind: HandleKind) -> ProblemError:
    """Return the ``410`` a handle of this kind answers when it cannot be used."""
    return errors.pin_expired() if kind == "plex_pin" else errors.quick_connect_expired()
