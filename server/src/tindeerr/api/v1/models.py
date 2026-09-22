"""Response and request bodies shared by the v1 routes (contract schemas).

One model per contract schema, named after it. Steps 2b and 2c reuse them, so the shapes
the console and the app see stay identical whichever endpoint answered.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tindeerr.auth.access import reauth_expires_at
from tindeerr.auth.methods import AuthMethod
from tindeerr.auth.sessions import CookieGrant
from tindeerr.auth.sessions import TokenPair as TokenPairValue
from tindeerr.ports.media_server import ConnectionCheck, ConnectorHealth, MediaServerKind
from tindeerr.storage.users import Role, User

#: A Plex PIN or Quick Connect handle, and the pairing code: 128 bits, base64url.
Handle = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{22,64}$")]


class UserResponse(BaseModel):
    """Contract schema ``User``."""

    id: str
    name: str
    role: Role
    media_server_admin: bool

    @classmethod
    def of(cls, user: User) -> "UserResponse":
        """Build the response for a user row."""
        return cls(
            id=user.id,
            name=user.name,
            role=user.role,
            media_server_admin=user.media_server_admin,
        )


class WebSessionResponse(BaseModel):
    """Contract schema ``WebSession``: what the console needs after a sign-in or a reload."""

    kind: Literal["web", "setup"]
    user: UserResponse | None = None
    csrf_token: str
    expires_at: datetime
    reauth_expires_at: datetime | None = None
    setup_completed_now: bool = False

    @classmethod
    def of(
        cls,
        grant: CookieGrant,
        user: User | None,
        now: datetime,
        *,
        setup_completed_now: bool = False,
    ) -> "WebSessionResponse":
        """Build the response for a session the caller just received or reloaded."""
        session = grant.session
        kind: Literal["web", "setup"] = "setup" if session.kind == "setup" else "web"
        return cls(
            kind=kind,
            user=None if user is None else UserResponse.of(user),
            csrf_token=grant.csrf_token,
            expires_at=session.expires_at,
            reauth_expires_at=reauth_expires_at(session, now),
            setup_completed_now=setup_completed_now,
        )


class TokenPairResponse(BaseModel):
    """Contract schema ``TokenPair``."""

    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime

    @classmethod
    def of(cls, tokens: TokenPairValue) -> "TokenPairResponse":
        """Build the response for a freshly issued pair."""
        return cls(
            access_token=tokens.access_token,
            access_expires_at=tokens.access_expires_at,
            refresh_token=tokens.refresh_token,
            refresh_expires_at=tokens.refresh_expires_at,
        )


class ConnectorStatusResponse(BaseModel):
    """Contract schema ``ConnectorStatus``: coarse health, never a response body."""

    health: ConnectorHealth
    server_name: str | None = None
    server_version: str | None = None
    checked_at: datetime

    @classmethod
    def of(cls, check: ConnectionCheck, checked_at: datetime) -> "ConnectorStatusResponse":
        """Build the response for a connection test."""
        return cls(
            health=check.health,
            server_name=check.server_name,
            server_version=check.server_version,
            checked_at=checked_at,
        )


class MediaServerInfo(BaseModel):
    """The configured media server, as ``ServerInfo`` and ``SetupState`` show it."""

    kind: MediaServerKind


class SetupStateResponse(BaseModel):
    """Contract schema ``SetupState``."""

    media_server: MediaServerInfo | None
    media_server_locked: bool
    locked_fields: list[str]
    auth_methods: list[AuthMethod]


class MediaServerConfigInput(BaseModel):
    """Contract schema ``MediaServerConfigInput``.

    Jellyfin and Emby take an administrator API key, Plex an owner-token PIN handle;
    neither accepts the other's field.
    """

    model_config = ConfigDict(extra="forbid")

    connector: Literal["media_server"]
    server_type: MediaServerKind
    url: Annotated[str, Field(max_length=512, pattern=r"^https?://")]
    api_key: Annotated[str | None, Field(default=None, min_length=1, max_length=512)] = None
    plex_pin_id: Handle | None = None
    verify_tls: bool = True

    @model_validator(mode="after")
    def _check_secret_matches_the_kind(self) -> "MediaServerConfigInput":
        if self.server_type == "plex" and self.api_key is not None:
            msg = "api_key: a Plex connector takes plex_pin_id, not an API key"
            raise ValueError(msg)
        if self.server_type != "plex" and self.plex_pin_id is not None:
            msg = "plex_pin_id: only a Plex connector takes a PIN handle"
            raise ValueError(msg)
        return self


class RefreshInput(BaseModel):
    """Body of ``POST /auth/refresh``."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: Annotated[str, Field(max_length=128)]


class SetupClaimInput(BaseModel):
    """Body of ``POST /setup/claim``."""

    model_config = ConfigDict(extra="forbid")

    setup_code: Annotated[str, Field(min_length=12, max_length=64)]
