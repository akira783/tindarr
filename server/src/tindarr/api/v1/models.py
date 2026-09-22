"""Response and request bodies shared by the v1 routes (contract schemas).

One model per contract schema, named after it. Steps 2b and 2c reuse them, so the shapes
the console and the app see stay identical whichever endpoint answered.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tindarr.auth.access import reauth_expires_at
from tindarr.auth.methods import AuthMethod
from tindarr.auth.sessions import CookieGrant
from tindarr.auth.sessions import TokenPair as TokenPairValue
from tindarr.ports.media_server import ConnectionCheck, ConnectorHealth, MediaServerKind
from tindarr.storage.sessions import Device
from tindarr.storage.users import Role, User

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


#: PKCE (RFC 7636): the app keeps the verifier and sends its S256 challenge.
CodeVerifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9._~-]{43,128}$")]
CodeChallenge = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
Username = Annotated[str, Field(min_length=1, max_length=128)]
Password = Annotated[str, Field(max_length=512)]


class DeviceInput(BaseModel):
    """Contract schema ``DeviceInput``: how the app names the phone it runs on."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(max_length=80)]
    platform: Literal["android", "ios"]
    app_version: Annotated[str, Field(max_length=32)]

    def as_device(self) -> Device:
        """Return the session row's device columns."""
        return Device(name=self.name, platform=self.platform, app_version=self.app_version)


class LoginInput(BaseModel):
    """Body of ``POST /auth/login`` (the app signs in with a password)."""

    model_config = ConfigDict(extra="forbid")

    username: Username
    password: Password
    device: DeviceInput


class WebLoginInput(BaseModel):
    """Body of ``POST /auth/web/login``; the console's device comes from its User-Agent."""

    model_config = ConfigDict(extra="forbid")

    username: Username
    password: Password


class HandlePurposeMixin(BaseModel):
    """The rule both handle requests share: ``code_challenge`` belongs to ``sign_in``.

    Without it a ``sign_in`` handle is the console's and is bound to its pre-auth
    cookie; with it, it is the app's and is bound to the PKCE challenge. A ``reauth`` or
    ``owner_token`` handle is bound to the session that created it, so a challenge would
    be meaningless there and is refused rather than ignored.
    """

    model_config = ConfigDict(extra="forbid")

    code_challenge: CodeChallenge | None = None

    @model_validator(mode="after")
    def _challenge_is_for_sign_in(self) -> "HandlePurposeMixin":
        purpose = getattr(self, "purpose", None)
        if self.code_challenge is not None and purpose != "sign_in":
            msg = "code_challenge: only a sign_in handle takes a PKCE challenge"
            raise ValueError(msg)
        return self


class PlexPinRequest(HandlePurposeMixin):
    """Contract schema ``PlexPinRequest``."""

    purpose: Literal["sign_in", "reauth", "owner_token"]


class QuickConnectRequest(HandlePurposeMixin):
    """Contract schema ``QuickConnectRequest`` (Quick Connect has no owner token)."""

    purpose: Literal["sign_in", "reauth"]


class PlexPinResponse(BaseModel):
    """Answer of ``POST /auth/plex/pins``: a handle, never the plex.tv PIN id."""

    pin_id: str
    auth_url: str
    expires_at: datetime


class QuickConnectResponse(BaseModel):
    """Answer of ``POST /auth/quick-connect``: a handle and the code to approve."""

    handle: str
    code: str
    expires_at: datetime


class PlexPinStatusInput(BaseModel):
    """Body of ``POST /auth/plex/pins/status``; a POST so no handle reaches a URL."""

    model_config = ConfigDict(extra="forbid")

    pin_id: Handle


class PlexPinStatusResponse(BaseModel):
    """Answer of ``POST /auth/plex/pins/status``."""

    status: Literal["pending", "authorized"]
    expires_at: datetime
    account_name: str | None = None


class PlexLoginInput(BaseModel):
    """Body of ``POST /auth/plex/login`` (the app proves the handle with PKCE)."""

    model_config = ConfigDict(extra="forbid")

    pin_id: Handle
    code_verifier: CodeVerifier
    device: DeviceInput


class WebPlexLoginInput(BaseModel):
    """Body of ``POST /auth/web/plex/login``; the pre-auth cookie proves the handle."""

    model_config = ConfigDict(extra="forbid")

    pin_id: Handle


class QuickConnectLoginInput(BaseModel):
    """Body of ``POST /auth/quick-connect/login``."""

    model_config = ConfigDict(extra="forbid")

    handle: Handle
    code_verifier: CodeVerifier
    device: DeviceInput


class WebQuickConnectLoginInput(BaseModel):
    """Body of ``POST /auth/web/quick-connect/login``."""

    model_config = ConfigDict(extra="forbid")

    handle: Handle


class ReauthInput(BaseModel):
    """Contract schema ``ReauthInput``: exactly one proof, checked on the current server."""

    model_config = ConfigDict(extra="forbid")

    password: Password | None = None
    pin_id: Handle | None = None
    handle: Handle | None = None

    @model_validator(mode="after")
    def _exactly_one_proof(self) -> "ReauthInput":
        given = [value for value in (self.password, self.pin_id, self.handle) if value is not None]
        if len(given) != 1:
            msg = "send exactly one of password, pin_id or handle"
            raise ValueError(msg)
        return self


class ReauthResponse(BaseModel):
    """Answer of ``POST /auth/web/reauth``."""

    reauth_expires_at: datetime


class PendingResponse(BaseModel):
    """Contract schema ``Pending``: the work is not finished, ask again."""

    pending: Literal[True] = True
    retry_after_ms: Annotated[int, Field(ge=250)]


class AuthResultResponse(BaseModel):
    """Contract schema ``AuthResult``: the app's token pair and who signed in."""

    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    user: UserResponse
    setup_completed_now: bool = False

    @classmethod
    def of(cls, tokens: TokenPairValue, user: User) -> "AuthResultResponse":
        """Build the answer of an app sign-in."""
        return cls(
            access_token=tokens.access_token,
            access_expires_at=tokens.access_expires_at,
            refresh_token=tokens.refresh_token,
            refresh_expires_at=tokens.refresh_expires_at,
            user=UserResponse.of(user),
        )


class SecretStateResponse(BaseModel):
    """Contract schema ``SecretState``: whether a secret is set, never its value."""

    set: bool
    last4: str | None = None
    locked: bool = False


class ConnectorResponse(BaseModel):
    """Contract schema ``Connector``, as far as step 2 fills it in."""

    kind: Literal["media_server", "requests", "tmdb", "omdb", "llm"]
    configured: bool
    provider: str | None = None
    url: str | None = None
    verify_tls: bool | None = None
    model: str | None = None
    tv_seasons: Literal["all", "first"] | None = None
    secret: SecretStateResponse
    locked_fields: list[str]
    status: ConnectorStatusResponse
