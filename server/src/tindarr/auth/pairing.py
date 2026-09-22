"""Connecting a phone from the console (docs/auth.md, section 9; ADR 0009).

Typing a server address and a password on a phone is the first thing a new user has to
get right, so Tindarr does it the other way round: someone already signed in on a
computer creates a pairing, the phone scans its QR code, and the user approves the
phone from the console.

What makes that safe is that **nothing is a credential on its own**:

- the **code** is 128 random bits, stored as a SHA-256, shown once, good for five
  minutes, and usable once. Anything wrong with it — unknown, expired, already used,
  already requested, cancelled — answers the same ``410``, so someone without a valid
  code learns nothing, not even whether one exists;
- the code alone never opens a session. The phone must also prove a **PKCE verifier**
  it kept to itself, and a human must approve in the console;
- the approval screen shows the **confirmation code** derived from the phone's
  challenge. A phone that raced on a stolen code shows a different number, so the user
  approving is the one who checks that the phone in their hand is the one asking;
- ``public_url`` must be set first: a QR code pointing nowhere useful is worse than no
  QR code, and the address in it is the one the phone will trust from then on.

Pairing never contacts the media server, so it does not re-sync the administrator flag
(ADR 0010): the web sign-in that created the pairing did.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tindarr.auth import access, errors
from tindarr.auth.events import security_event
from tindarr.auth.handles import pkce_challenge
from tindarr.auth.sessions import MobileGrant, SessionService
from tindarr.auth.tokens import token_hash, tokens_equal
from tindarr.core.clock import Clock
from tindarr.core.errors import PendingError, ProblemError
from tindarr.storage import pairings as repository
from tindarr.storage import users as user_repository
from tindarr.storage.db import write_transaction
from tindarr.storage.pairings import Pairing
from tindarr.storage.sessions import Device
from tindarr.storage.users import User

#: A pairing is good for five minutes from its creation, whatever happens in between.
PAIRING_LIFETIME: Final = timedelta(minutes=5)
#: At most this many unfinished pairings per user (docs/auth.md, section 8).
MAX_OPEN_PAIRINGS: Final = 3
#: How often the app polls for the approval, and the console for the request.
POLL_INTERVAL_MS: Final = 2000
#: 128 bits, base64url: 22 characters, the contract's ``PairingCode``.
_CODE_BYTES: Final = 16
_CONFIRMATION_MODULUS: Final = 10_000
_CONFIRMATION_BYTES: Final = 4
#: Scheme the app registers; the console renders this link as the QR code.
PAIRING_LINK_SCHEME: Final = "tindarr://pair"


def new_pairing_code() -> str:
    """Return a fresh pairing code: 128 random bits, base64url."""
    return secrets.token_urlsafe(_CODE_BYTES)


def confirmation_code(code_challenge: str) -> str:
    """Return the four digits both screens show (docs/auth.md, section 9).

    The first 32 bits of the challenge's SHA-256, modulo 10 000. It is derived, not
    drawn: the phone can compute it from what it already holds, so it never travels.
    """
    digest = hashlib.sha256(code_challenge.encode()).digest()
    value = int.from_bytes(digest[:_CONFIRMATION_BYTES], "big") % _CONFIRMATION_MODULUS
    return f"{value:0{len(str(_CONFIRMATION_MODULUS)) - 1}d}"


def pairing_link(public_url: str, code: str) -> str:
    """Return the link the QR code carries."""
    return f"{PAIRING_LINK_SCHEME}?{urlencode({'server': public_url, 'code': code})}"


@dataclass(frozen=True, slots=True)
class NewPairing:
    """A pairing just created: the row, the code shown once, and the link."""

    pairing: Pairing
    code: str
    link: str


@dataclass(frozen=True, slots=True)
class PairingPreview:
    """What the app may show before the user confirms: names and the deadline."""

    server_name: str
    user_name: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PairingRequested:
    """The answer to a phone's request: the four digits, and when it all expires."""

    confirmation_code: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PairingCompleted:
    """The answer to a phone that collected its tokens."""

    grant: MobileGrant
    user: User


class PairingService:
    """Creates, approves, rejects and completes pairings."""

    def __init__(self, engine: AsyncEngine, clock: Clock, sessions: SessionService) -> None:
        self._engine = engine
        self._clock = clock
        self._sessions = sessions

    # --- the console side -----------------------------------------------------------

    async def create(self, user: User, public_url: str | None) -> NewPairing:
        """Create a pairing for this user and return its code, once.

        Raises ``409 public_url_not_set`` without an address to put in the QR code, and
        ``429`` past the per-user cap (which is what stops one account from filling the
        table with codes).
        """
        if public_url is None:
            raise errors.public_url_not_set()
        now = self._clock.now()
        code = new_pairing_code()
        async with write_transaction(self._engine) as connection:
            open_pairings = await repository.list_open_for_user(connection, user.id, now)
            if len(open_pairings) >= MAX_OPEN_PAIRINGS:
                # The wait is until the oldest one runs out: the cap frees itself.
                soonest = min(item.expires_at for item in open_pairings)
                raise errors.too_many_pairings(_milliseconds_until(soonest, now))
            pairing = await repository.insert(
                connection,
                repository.new_pairing(
                    user.id, token_hash(code), now=now, expires_at=now + PAIRING_LIFETIME
                ),
            )
        security_event("pairing_created", user_id=user.id, pairing_id=pairing.id)
        return NewPairing(pairing, code, pairing_link(public_url, code))

    async def owned(self, pairing_id: str, user: User) -> Pairing:
        """Return one of the caller's pairings, or ``404``.

        Another user's pairing is ``404`` too: the console must not be able to tell
        "there is no such pairing" from "there is one, but not yours".
        """
        async with self._engine.connect() as connection:
            pairing = await repository.get(connection, pairing_id)
        if pairing is None or pairing.user_id != user.id:
            raise errors.not_found("No such pairing.")
        return pairing

    async def approve(self, pairing_id: str, user: User) -> Pairing:
        """Approve the phone waiting on this pairing.

        Only from ``awaiting_approval``: approving before a phone has asked would
        approve whichever phone turns up next.
        """
        pairing = await self.owned(pairing_id, user)
        now = self._clock.now()
        if pairing.expired(now):
            raise errors.pairing_expired()
        if pairing.status != "awaiting_approval":
            raise errors.pairing_not_awaiting_approval()
        async with write_transaction(self._engine) as connection:
            moved = await repository.transition(
                connection,
                pairing.id,
                expected=("awaiting_approval",),
                to="approved",
                now=now,
                values={"approved_at": now},
            )
            if not moved:
                raise errors.pairing_not_awaiting_approval()
            approved = await repository.get(connection, pairing.id)
        security_event("pairing_approved", user_id=user.id, pairing_id=pairing.id)
        return approved or pairing

    async def revoke(self, pairing_id: str, user: User) -> None:
        """Cancel, reject, or sign out the phone this pairing connected.

        One verb for the three, because they are one thing from the console: "I do not
        want this connection". A completed pairing revokes the session it opened.
        """
        pairing = await self.owned(pairing_id, user)
        now = self._clock.now()
        if pairing.status == "completed":
            if pairing.session_id is not None:
                await self._sessions.revoke(pairing.session_id, "user")
            security_event("pairing_session_revoked", user_id=user.id, pairing_id=pairing.id)
            return
        async with write_transaction(self._engine) as connection:
            await repository.transition(
                connection,
                pairing.id,
                expected=repository.OPEN_STATUSES,
                to="revoked",
                now=now,
                values={"revoked_at": now},
                require_unexpired=False,
            )
        security_event(
            "pairing_revoked",
            user_id=user.id,
            pairing_id=pairing.id,
            rejected=pairing.was_requested,
        )

    # --- the app side ---------------------------------------------------------------

    async def preview(self, code: str, server_name: str) -> PairingPreview:
        """Return what the phone must show before the user confirms.

        Only for a ``pending``, unexpired code. Everything else is the same ``410`` an
        unknown code gets.
        """
        now = self._clock.now()
        async with self._engine.connect() as connection:
            pairing = await self._pending(connection, code, now)
            owner = await user_repository.get(connection, pairing.user_id)
        if owner is None or not owner.can_sign_in:  # pragma: no cover - cascade removes them
            raise errors.pairing_expired()
        return PairingPreview(server_name, owner.name, pairing.expires_at)

    async def request(
        self, code: str, *, device: Device, code_challenge: str, client_ip: str | None
    ) -> PairingRequested:
        """Record a phone's request and return the four digits it must show.

        The code stops working for preview and request here: one code, one phone.
        """
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            pairing = await self._pending(connection, code, now)
            moved = await repository.transition(
                connection,
                pairing.id,
                expected=("pending",),
                to="awaiting_approval",
                now=now,
                values={
                    "code_challenge": code_challenge,
                    "device_name": device.name,
                    "platform": device.platform,
                    "app_version": device.app_version,
                    "requested_from": client_ip,
                    "requested_at": now,
                },
            )
            if not moved:  # pragma: no cover - another phone won the same transaction
                raise errors.pairing_expired()
        security_event("pairing_requested", pairing_id=pairing.id, client=client_ip)
        return PairingRequested(confirmation_code(code_challenge), pairing.expires_at)

    async def complete(
        self, code: str, code_verifier: str, *, client_is_private: bool
    ) -> PairingCompleted:
        """Hand the phone its tokens, once the user approved it.

        Raises ``202`` (``PendingError``) while it waits, ``403 pairing_rejected`` once
        the user said no, and ``410`` for everything else — including a verifier that
        does not match the challenge, which is checked **before** anything else is
        revealed about the pairing's fate.
        """
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            pairing = await self._by_code(connection, code, now)
            self._require_verifier(pairing, code_verifier)
            if pairing.status == "awaiting_approval":
                raise PendingError(POLL_INTERVAL_MS)
            if pairing.status == "revoked":
                # It was rejected in the console; the app says so and stops polling.
                raise errors.pairing_rejected()
            if pairing.status != "approved":
                raise errors.pairing_expired()
            completed = await self._open_session(
                connection, pairing, now, client_is_private=client_is_private
            )
        security_event("pairing_completed", user_id=completed.user.id, pairing_id=pairing.id)
        return completed

    # --- the pieces the three app calls share ---------------------------------------

    async def _by_code(self, connection: AsyncConnection, code: str, now: datetime) -> Pairing:
        pairing = await repository.get_by_code_hash(connection, token_hash(code))
        if pairing is None or pairing.expired(now) or pairing.status == "completed":
            raise errors.pairing_expired()
        return pairing

    async def _pending(self, connection: AsyncConnection, code: str, now: datetime) -> Pairing:
        pairing = await self._by_code(connection, code, now)
        if pairing.status != "pending":
            raise errors.pairing_expired()
        return pairing

    @staticmethod
    def _require_verifier(pairing: Pairing, code_verifier: str) -> None:
        """Refuse a verifier that is not the one behind the recorded challenge."""
        if pairing.code_challenge is None or not tokens_equal(
            pkce_challenge(code_verifier), pairing.code_challenge
        ):
            raise errors.pairing_expired()

    async def _open_session(
        self,
        connection: AsyncConnection,
        pairing: Pairing,
        now: datetime,
        *,
        client_is_private: bool,
    ) -> PairingCompleted:
        user = await user_repository.get(connection, pairing.user_id)
        if user is None:  # pragma: no cover - the cascade removes the pairing with them
            raise errors.pairing_expired()
        # The same checks as any authenticated request: a user disabled or restricted
        # since the pairing was created does not get a session out of it.
        self._require_usable(user, client_is_private=client_is_private)
        grant = await self._sessions.open_mobile_session(connection, user, pairing.device)
        moved = await repository.transition(
            connection,
            pairing.id,
            expected=("approved",),
            to="completed",
            now=now,
            values={"completed_at": now, "session_id": grant.session.id},
        )
        if not moved:  # pragma: no cover - another poll won the same transaction
            raise errors.pairing_expired()
        return PairingCompleted(grant, user)

    @staticmethod
    def _require_usable(user: User, *, client_is_private: bool) -> None:
        try:
            access.require_usable_account(user)
        except ProblemError:
            # ``401`` would tell the app to sign in again with a credential it does not
            # have; the contract answers ``403 account_disabled`` here.
            raise errors.account_disabled() from None
        access.require_remote_access(user, client_is_private=client_is_private)


def _milliseconds_until(deadline: datetime, now: datetime) -> int:
    """How long to wait before trying again, never less than a second."""
    return max(int((deadline - now).total_seconds() * 1000), 1000)
