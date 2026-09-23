"""Response and request bodies shared by the v1 routes (contract schemas).

One model per contract schema, named after it. Steps 2b and 2c reuse them, so the shapes
the console and the app see stay identical whichever endpoint answered.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from tindarr.auth.access import reauth_expires_at
from tindarr.auth.mediaserver import LockedMediaServerValues
from tindarr.auth.methods import AuthMethod
from tindarr.auth.pairing import POLL_INTERVAL_MS, NewPairing, confirmation_code
from tindarr.auth.sessions import CookieGrant
from tindarr.auth.sessions import TokenPair as TokenPairValue
from tindarr.connectors import ConnectorInput
from tindarr.core.net import normalize_public_url
from tindarr.ports.llm import LlmProviderKind, ReasoningEffort
from tindarr.ports.media_server import ConnectionCheck, ConnectorHealth, MediaServerKind
from tindarr.ports.request_backend import SeasonPolicy
from tindarr.storage.pairings import Pairing, PairingState
from tindarr.storage.sessions import Device, Session
from tindarr.storage.settings import (
    ContentFilters,
    PasswordSignIn,
    SettingsStore,
    StreamingRegion,
)
from tindarr.storage.users import DisabledReason, Role, User

#: A Plex PIN or Quick Connect handle, and the pairing code: 128 bits, base64url.
Handle = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{22,64}$")]
#: Contract schema ``PairingCode``, the same shape as a handle.
PairingCode = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{22,64}$")]
#: Pairing states nothing will move away from, so the console stops polling.
FINAL_PAIRING_STATES: Final = frozenset({"completed", "expired", "revoked"})


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


class AdminUserResponse(UserResponse):
    """Contract schema ``AdminUser``: what the console's user list shows.

    ``votes`` and ``generations_today`` are zero until the swipe engine counts them
    (step 4), and ``request_backend_user_found`` is left out until step 3 brings the
    request backend: the contract makes both optional for exactly that reason.
    """

    enabled: bool
    disabled_reason: DisabledReason | None = None
    promoted: bool
    remote_access: bool
    created_at: datetime
    last_sign_in_at: datetime | None = None
    last_seen_at: datetime | None = None
    daily_generation_limit: int | None = None
    votes: int = 0
    generations_today: int = 0

    @classmethod
    def of(cls, user: User) -> "AdminUserResponse":
        """Build the response for a user row."""
        return cls(
            id=user.id,
            name=user.name,
            role=user.role,
            media_server_admin=user.media_server_admin,
            enabled=user.enabled,
            disabled_reason=user.disabled_reason,
            promoted=user.promoted,
            remote_access=user.remote_access,
            created_at=user.created_at,
            last_sign_in_at=user.last_sign_in_at,
            last_seen_at=user.last_seen_at,
            daily_generation_limit=user.daily_generation_limit,
        )


class SessionResponse(BaseModel):
    """Contract schema ``Session``: one of the caller's devices.

    ``device_name`` and ``platform`` are nullable because the columns are: the app
    always names its device and a console session is named after its browser, but a row
    written without either must be listable all the same (docs/auth.md, section 12).
    """

    id: str
    kind: Literal["mobile", "web"]
    device_name: str | None
    platform: str | None
    app_version: str | None = None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    current: bool

    @classmethod
    def of(cls, session: Session, *, current: bool) -> "SessionResponse":
        """Build the response for a session row (never a ``setup`` one)."""
        kind: Literal["mobile", "web"] = "mobile" if session.kind == "mobile" else "web"
        return cls(
            id=session.id,
            kind=kind,
            device_name=session.device_name,
            platform=session.platform,
            app_version=session.app_version,
            created_at=session.created_at,
            last_seen_at=session.last_seen_at,
            expires_at=session.expires_at,
            current=current,
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
    """Contract schema ``ConnectorStatus``: coarse health, never a response body.

    An adapter only ever reports the five health values of the port. Listing the
    connectors adds the two the contract has for connectors nothing called:
    ``not_configured`` and ``unknown`` (configured, but not tested by this answer —
    listing them must not call every remote service).
    """

    health: ConnectorHealth | Literal["not_configured", "unknown"]
    server_name: str | None = None
    server_version: str | None = None
    checked_at: datetime | None = None

    @classmethod
    def of(cls, check: ConnectionCheck, checked_at: datetime) -> "ConnectorStatusResponse":
        """Build the response for a connection test."""
        return cls(
            health=check.health,
            server_name=check.server_name,
            server_version=check.server_version,
            checked_at=checked_at,
        )

    @classmethod
    def untested(cls, *, configured: bool) -> "ConnectorStatusResponse":
        """Build the status of a connector this answer did not test."""
        return cls(health="unknown" if configured else "not_configured")


class MediaServerInfo(BaseModel):
    """The configured media server, as ``ServerInfo`` and ``SetupState`` show it."""

    kind: MediaServerKind
    #: What the server calls itself, from the last successful connection test.
    name: str | None = None


async def media_server_info(
    settings: SettingsStore, kind: MediaServerKind | None
) -> MediaServerInfo | None:
    """Describe the configured media server, or ``None`` when there is none."""
    if kind is None:
        return None
    name = (await settings.get("media_server_name")).value
    return MediaServerInfo(kind=kind, name=name if isinstance(name, str) and name else None)


class LockedValuesResponse(BaseModel):
    """Contract schema ``SetupState.locked_values``: never a secret."""

    server_type: MediaServerKind | None = None
    url: str | None = None
    verify_tls: bool | None = None

    @classmethod
    def of(cls, values: LockedMediaServerValues) -> "LockedValuesResponse":
        """Build the response for the values the environment forces."""
        return cls(server_type=values.server_type, url=values.url, verify_tls=values.verify_tls)


class SetupStateResponse(BaseModel):
    """Contract schema ``SetupState``."""

    media_server: MediaServerInfo | None
    media_server_locked: bool
    locked_fields: list[str]
    locked_values: LockedValuesResponse
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


class ApiKeyInput(BaseModel):
    """Contract schema ``ApiKeyInput``: TMDb and OMDb, which are a key and nothing else."""

    model_config = ConfigDict(extra="forbid")

    connector: Literal["tmdb", "omdb"]
    api_key: Annotated[str | None, Field(default=None, min_length=1, max_length=512)] = None


class RequestsInput(BaseModel):
    """Contract schema ``RequestsInput``: Seerr, Jellyseerr or Overseerr."""

    model_config = ConfigDict(extra="forbid")

    connector: Literal["requests"]
    url: Annotated[str, Field(max_length=512, pattern=r"^https?://")]
    api_key: Annotated[str | None, Field(default=None, min_length=1, max_length=512)] = None
    verify_tls: bool = True
    tv_seasons: SeasonPolicy = "all"


class LlmSettingsInput(BaseModel):
    """Contract schema ``LlmSettingsInput``: the AI provider and how to reach it."""

    # ``model`` is a contract field name, and pydantic reserves the ``model_`` prefix
    # for its own methods; the namespace is opened deliberately rather than renaming a
    # field the app and the console already know.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    connector: Literal["llm"]
    provider: LlmProviderKind
    api_key: Annotated[str | None, Field(default=None, min_length=1, max_length=512)] = None
    base_url: Annotated[str | None, Field(default=None, max_length=512, pattern=r"^https?://")] = (
        None
    )
    model: Annotated[str | None, Field(default=None, min_length=1, max_length=128)] = None
    reasoning_effort: ReasoningEffort | None = None


#: Contract schema ``ConnectorInput``: one body per connector kind, told apart by the
#: ``connector`` field, which must also match the path.
type ConnectorInputBody = Annotated[
    MediaServerConfigInput | LlmSettingsInput | RequestsInput | ApiKeyInput,
    Field(discriminator="connector"),
]


#: The four bodies this service owns; the media server keeps its own route.
type OptionalConnectorBody = LlmSettingsInput | RequestsInput | ApiKeyInput


def as_connector_input(payload: OptionalConnectorBody) -> ConnectorInput:
    """Turn an optional connector's body into the values the service works with.

    ``model_fields_set`` is what says which fields the request really carried, so an
    omitted one keeps its stored value and is never checked against a lock.
    """
    given = frozenset(payload.model_fields_set) - {"connector"}
    if isinstance(payload, ApiKeyInput):
        return ConnectorInput(kind=payload.connector, api_key=payload.api_key, given=given)
    if isinstance(payload, RequestsInput):
        return ConnectorInput(
            kind="requests",
            url=payload.url,
            api_key=payload.api_key,
            verify_tls=payload.verify_tls,
            tv_seasons=payload.tv_seasons,
            given=given,
        )
    return ConnectorInput(
        kind="llm",
        provider=payload.provider,
        api_key=payload.api_key,
        base_url=payload.base_url,
        model=payload.model,
        reasoning_effort=payload.reasoning_effort,
        given=given,
    )


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


class LlmModelsResponse(BaseModel):
    """Answer of ``POST /admin/llm/models``: model ids only, never a provider's prose."""

    models: list[str]


class ConnectorListResponse(BaseModel):
    """Answer of ``GET /admin/connectors``."""

    connectors: list["ConnectorResponse"]


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


#: Contract field of ``ServerSettings`` -> the setting that stores it.
SETTING_FOR_SETTINGS_FIELD: Final[Mapping[str, str]] = {
    "name": "server_name",
    "public_url": "public_url",
    "password_sign_in": "password_sign_in",
    "language": "language",
    "streaming_region": "streaming_region",
    "daily_generation_limit": "daily_generation_limit",
    "warm_up_enabled": "warm_up_enabled",
    "content_filters": "content_filters",
}
#: Fields only a media server administrator may change (docs/auth.md, section 7).
MEDIA_SERVER_ADMIN_FIELDS: Final = frozenset({"public_url", "password_sign_in"})
#: Fields that also need a re-authentication less than five minutes old.
REAUTH_FIELDS: Final = frozenset({"public_url"})
#: The two settings the contract lets an administrator clear with ``null``.
NULLABLE_SETTINGS_FIELDS: Final = frozenset({"public_url", "streaming_region"})

#: ``public_url`` as the contract validates it, then normalised to a bare origin.
PublicUrlInput = Annotated[str, Field(max_length=255), AfterValidator(normalize_public_url)]


class ServerSettingsResponse(BaseModel):
    """Contract schema ``ServerSettings``: every field is stored, some act now."""

    name: str
    public_url: str | None
    password_sign_in: PasswordSignIn
    language: str
    streaming_region: str | None
    daily_generation_limit: int
    warm_up_enabled: bool
    content_filters: ContentFilters
    locked_fields: list[str]


async def read_server_settings(store: SettingsStore) -> ServerSettingsResponse:
    """Read every general setting, with the fields an environment variable locks.

    ``SettingsStore.get`` already returns a value of the setting's declared type (or
    its default), so the model only has to name them.
    """
    values: dict[str, object] = {
        field: (await store.get(setting)).value
        for field, setting in SETTING_FOR_SETTINGS_FIELD.items()
    }
    values["locked_fields"] = [
        field for field, setting in SETTING_FOR_SETTINGS_FIELD.items() if store.is_locked(setting)
    ]
    return ServerSettingsResponse.model_validate(values)


class ServerSettingsPatchInput(BaseModel):
    """Contract schema ``ServerSettingsPatch``: only the fields the caller sent.

    Which fields were sent is what the rules are applied to, so ``None`` has to mean
    "cleared" for the two settings the contract makes nullable and nothing at all for
    the others; ``model_fields_set`` tells the two apart.
    """

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(max_length=60)] | None = None
    public_url: PublicUrlInput | None = None
    password_sign_in: PasswordSignIn | None = None
    language: Annotated[str, Field(max_length=16)] | None = None
    streaming_region: StreamingRegion | None = None
    daily_generation_limit: Annotated[int, Field(ge=0)] | None = None
    warm_up_enabled: bool | None = None
    content_filters: ContentFilters | None = None

    @model_validator(mode="after")
    def _at_least_one_field_and_no_stray_nulls(self) -> "ServerSettingsPatchInput":
        if not self.model_fields_set:
            msg = "send at least one setting to change"
            raise ValueError(msg)
        for field in self.model_fields_set - NULLABLE_SETTINGS_FIELDS:
            if getattr(self, field) is None:
                msg = f"{field}: this setting cannot be cleared"
                raise ValueError(msg)
        return self

    @property
    def changes(self) -> dict[str, object]:
        """The settings to write, by setting name, in the order the contract lists them."""
        return {
            SETTING_FOR_SETTINGS_FIELD[field]: getattr(self, field)
            for field in SETTING_FOR_SETTINGS_FIELD
            if field in self.model_fields_set
        }


ConnectorListResponse.model_rebuild()


class PairingDeviceResponse(BaseModel):
    """The phone that asked to pair, as the console shows it."""

    name: str
    platform: str
    app_version: str | None = None


class PairingResponse(BaseModel):
    """Contract schema ``Pairing``: one of the caller's pairings."""

    id: str
    status: PairingState
    created_at: datetime
    expires_at: datetime
    requested_at: datetime | None = None
    approved_at: datetime | None = None
    completed_at: datetime | None = None
    device: PairingDeviceResponse | None = None
    requested_from: str | None = None
    confirmation_code: str | None = None
    session_id: str | None = None
    #: How long the console should wait before polling again; null once it is final.
    retry_after_ms: int | None = None

    @classmethod
    def of(cls, pairing: Pairing, now: datetime) -> "PairingResponse":
        """Build the response for a pairing row, as it stands at ``now``."""
        state = pairing.state(now)
        device = pairing.device
        return cls(
            id=pairing.id,
            status=state,
            created_at=pairing.created_at,
            expires_at=pairing.expires_at,
            requested_at=pairing.requested_at,
            approved_at=pairing.approved_at,
            completed_at=pairing.completed_at,
            device=None
            if device.name is None or device.platform is None
            else PairingDeviceResponse(
                name=device.name, platform=device.platform, app_version=device.app_version
            ),
            requested_from=pairing.requested_from,
            # Only while a phone is waiting: it is what the user checks against the screen.
            confirmation_code=confirmation_code(pairing.code_challenge)
            if pairing.code_challenge is not None and state == "awaiting_approval"
            else None,
            session_id=pairing.session_id,
            retry_after_ms=None if state in FINAL_PAIRING_STATES else POLL_INTERVAL_MS,
        )


class NewPairingResponse(PairingResponse):
    """Contract schema ``NewPairing``: the pairing, plus the code shown exactly once."""

    code: str
    link: str

    @classmethod
    def created(cls, created: NewPairing, now: datetime) -> "NewPairingResponse":
        """Build the answer of ``POST /pairings``."""
        return cls(
            **PairingResponse.of(created.pairing, now).model_dump(),
            code=created.code,
            link=created.link,
        )


class PairingPreviewResponse(BaseModel):
    """Answer of ``POST /auth/pair/preview``: only what the phone must display."""

    server_name: str
    user_name: str
    expires_at: datetime


class PairingRequestedResponse(BaseModel):
    """Contract schema ``PairingRequested``: waiting for the console's approval."""

    pending: Literal[True] = True
    retry_after_ms: int = POLL_INTERVAL_MS
    confirmation_code: str
    expires_at: datetime


class PairingCodeInput(BaseModel):
    """Body of ``POST /auth/pair/preview``."""

    model_config = ConfigDict(extra="forbid")

    code: PairingCode


class PairDeviceInput(BaseModel):
    """Body of ``POST /auth/pair``: the code, the phone, and its PKCE challenge."""

    model_config = ConfigDict(extra="forbid")

    code: PairingCode
    device: DeviceInput
    code_challenge: CodeChallenge


class CompletePairingInput(BaseModel):
    """Body of ``POST /auth/pair/complete``: the code and the verifier behind it."""

    model_config = ConfigDict(extra="forbid")

    code: PairingCode
    code_verifier: CodeVerifier
