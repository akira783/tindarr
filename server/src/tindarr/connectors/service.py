"""Reading, testing, saving and removing the four optional connectors.

One service for four kinds, because the interesting part is identical for all of them
and the differences are a table of field names. That part is the **secret-reuse rule**:
when a request omits a secret, the stored one is used only if the connector still points
at the same place. A change of address, or of the provider that decides the address, has
to come with the key again (``secret_required``).

The rule is not a formality. An administrator's session is the one credential that can
make this server call an address of somebody's choosing, and Tindarr holds an AI key
that costs money, a request-backend key that can fill a disk, and two metadata keys. If
an omitted secret were simply reused, a single stolen console session could point each
connector at a collector and read them all out of the request it sent.

Where there is no address, there is no rule to apply: TMDb and OMDb each have exactly
one host, written into their adapter, and nothing an administrator types can change it.

Nothing is stored before its connection test passes, and no test answer carries more
than a coarse health value.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Final, Literal, Protocol, get_args

from tindarr.core.errors import ProblemError
from tindarr.ports import problems
from tindarr.ports.connectors import ConnectionCheck
from tindarr.ports.factories import ConnectorFactories
from tindarr.ports.llm import (
    PROVIDERS_NEEDING_KEY,
    LlmConnection,
    LlmProvider,
    LlmProviderKind,
    ReasoningEffort,
)
from tindarr.ports.media_server import MediaServerKind, as_media_server_kind
from tindarr.ports.metadata import Metadata, RatingsSource
from tindarr.ports.request_backend import (
    RequestBackend,
    RequestBackendConnection,
    SeasonPolicy,
)
from tindarr.storage.settings import (
    SecretPinnedToItsAddressError,
    SettingLockedError,
    SettingsStore,
)

#: The connectors this service owns. ``media_server`` is not one of them.
type OptionalConnectorKind = Literal["tmdb", "omdb", "requests", "llm"]
OPTIONAL_CONNECTOR_KINDS: Final[tuple[OptionalConnectorKind, ...]] = get_args(
    OptionalConnectorKind.__value__
)

#: ``kind -> {contract field: setting name}``. This table is the only thing that differs
#: between the four connectors, and it is what decides which fields can be locked by an
#: environment variable and which a request may carry.
SETTING_FOR_FIELD: Final[Mapping[OptionalConnectorKind, Mapping[str, str]]] = {
    "tmdb": {"api_key": "tmdb_api_key"},
    "omdb": {"api_key": "omdb_api_key"},
    "requests": {
        "url": "requests_url",
        "api_key": "requests_api_key",
        "verify_tls": "requests_verify_tls",
        "tv_seasons": "requests_tv_seasons",
    },
    "llm": {
        "provider": "llm_provider",
        "api_key": "llm_api_key",
        "base_url": "llm_base_url",
        "model": "llm_model",
        "reasoning_effort": "llm_reasoning_effort",
    },
}
#: The AI providers that need no key of their own.
#: Read from the port, so the save rule and the adapter can never disagree again.
_KEYLESS_PROVIDERS: Final = frozenset(get_args(LlmProviderKind.__value__)) - PROVIDERS_NEEDING_KEY
LLM_PROVIDER_KINDS: Final[tuple[LlmProviderKind, ...]] = get_args(LlmProviderKind.__value__)
REASONING_EFFORTS: Final[tuple[ReasoningEffort, ...]] = get_args(ReasoningEffort.__value__)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MetadataSettings:
    """A stored TMDb or OMDb connector: a key and nothing else."""

    api_key: str


@dataclass(frozen=True, slots=True)
class RequestsSettings:
    """A stored request backend connector."""

    url: str
    api_key: str
    verify_tls: bool = True
    tv_seasons: SeasonPolicy = "all"


@dataclass(frozen=True, slots=True)
class LlmSettings:
    """A stored AI provider connector."""

    provider: LlmProviderKind
    api_key: str = ""
    base_url: str | None = None
    model: str = ""
    reasoning_effort: ReasoningEffort | None = None

    @property
    def connection(self) -> LlmConnection:
        """Return the connection an adapter is built from."""
        return LlmConnection(
            kind=self.provider,
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
        )


@dataclass(frozen=True, slots=True)
class ConnectorState:
    """A configured connector, as the console shows it (the secret stays masked there)."""

    kind: OptionalConnectorKind
    secret: str
    provider: str | None = None
    url: str | None = None
    verify_tls: bool | None = None
    model: str | None = None
    tv_seasons: SeasonPolicy | None = None


@dataclass(frozen=True, slots=True)
class ConnectorInput:
    """A ``ConnectorInput`` from the contract, as values.

    ``given`` lists the fields the request actually carried, so an omitted field is
    neither checked against a lock nor written, exactly as for the media server.
    """

    kind: OptionalConnectorKind
    api_key: str | None = None
    url: str | None = None
    verify_tls: bool | None = None
    tv_seasons: SeasonPolicy | None = None
    provider: LlmProviderKind | None = None
    base_url: str | None = None
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    given: frozenset[str] = field(default_factory=frozenset[str])

    def value_of(self, field_name: str) -> object:
        """Return the value this request carries for a contract field."""
        return getattr(self, field_name)


class ConnectorService:
    """Reads, tests, saves and removes the optional connectors."""

    def __init__(self, settings: SettingsStore, factories: ConnectorFactories) -> None:
        self._settings = settings
        self._factories = factories

    async def media_server_kind(self) -> MediaServerKind:
        """Which stored id a request-backend user is matched on.

        Read at every use rather than held, because repointing the media server changes
        it and the request backend must follow without a restart. Before the media
        server is configured there is nothing to match, and the value only reaches
        ``find_user``; a connection test never uses it.
        """
        return as_media_server_kind((await self._settings.get("media_server_kind")).value) or (
            "jellyfin"
        )

    # --- reading --------------------------------------------------------------------

    async def _text(self, name: str) -> str | None:
        value = (await self._settings.get(name)).value
        return value if isinstance(value, str) and value else None

    async def tmdb(self) -> MetadataSettings | None:
        """Return the stored TMDb connector, or ``None``."""
        key = await self._text("tmdb_api_key")
        return MetadataSettings(key) if key else None

    async def omdb(self) -> MetadataSettings | None:
        """Return the stored OMDb connector, or ``None``."""
        key = await self._text("omdb_api_key")
        return MetadataSettings(key) if key else None

    async def requests(self) -> RequestsSettings | None:
        """Return the stored request backend connector, or ``None`` if incomplete."""
        url = await self._text("requests_url")
        key = await self._text("requests_api_key")
        if url is None or key is None:
            return None
        seasons = (await self._settings.get("requests_tv_seasons")).value
        return RequestsSettings(
            url=url,
            api_key=key,
            verify_tls=bool((await self._settings.get("requests_verify_tls")).value),
            tv_seasons="first" if seasons == "first" else "all",
        )

    async def llm(self) -> LlmSettings | None:
        """Return the stored AI provider connector, or ``None`` if incomplete."""
        provider = await self._text("llm_provider")
        if provider is None:
            return None
        kind = _as_provider(provider)
        if kind is None:  # pragma: no cover - the setting's own type already checks it
            return None
        effort = await self._text("llm_reasoning_effort")
        return LlmSettings(
            provider=kind,
            api_key=await self._text("llm_api_key") or "",
            base_url=await self._text("llm_base_url"),
            model=await self._text("llm_model") or "",
            reasoning_effort=_as_effort(effort),
        )

    async def state(self, kind: OptionalConnectorKind) -> ConnectorState | None:
        """Describe a configured connector, or return ``None`` when there is none."""
        if kind in ("tmdb", "omdb"):
            found = await (self.tmdb() if kind == "tmdb" else self.omdb())
            return ConnectorState(kind, found.api_key) if found is not None else None
        if kind == "requests":
            backend = await self.requests()
            if backend is None:
                return None
            return ConnectorState(
                kind,
                backend.api_key,
                url=backend.url,
                verify_tls=backend.verify_tls,
                tv_seasons=backend.tv_seasons,
            )
        provider = await self.llm()
        if provider is None:
            return None
        return ConnectorState(
            kind,
            provider.api_key,
            provider=provider.provider,
            url=provider.base_url,
            model=provider.model or None,
        )

    def locked_values(self, kind: OptionalConnectorKind) -> dict[str, object]:
        """Return what an environment variable forces, among the fields the console shows.

        The API key is deliberately absent: a pinned secret is shown as pinned, never
        returned. The rest has to be shown, because a console that cannot display a
        pinned address cannot let anybody finish configuring that connector either.
        """
        return {
            name: self._settings.locked_value(setting)
            for name, setting in SETTING_FOR_FIELD[kind].items()
            if name != "api_key" and self._settings.is_locked(setting)
        }

    def locked_fields(self, kind: OptionalConnectorKind) -> list[str]:
        """Contract fields an environment variable sets, and so refuses to change."""
        return [
            name
            for name, setting in SETTING_FOR_FIELD[kind].items()
            if self._settings.is_locked(setting)
        ]

    # --- building the adapters (used by the swipe engine, from step 4) --------------

    async def metadata(self) -> Metadata | None:
        """Return the TMDb adapter, or ``None`` when no key is stored."""
        found = await self.tmdb()
        return self._factories.metadata(found.api_key) if found is not None else None

    async def ratings(self) -> RatingsSource | None:
        """Return the OMDb adapter, or ``None`` when no key is stored."""
        found = await self.omdb()
        return self._factories.ratings(found.api_key) if found is not None else None

    async def request_backend(self) -> RequestBackend | None:
        """Return the request backend adapter, or ``None`` when none is configured."""
        found = await self.requests()
        if found is None:
            return None
        return self._factories.request_backend(
            RequestBackendConnection(
                url=found.url,
                api_key=found.api_key,
                media_server_kind=await self.media_server_kind(),
                tv_seasons=found.tv_seasons,
                verify_tls=found.verify_tls,
            )
        )

    async def llm_provider(self) -> LlmProvider | None:
        """Return the AI provider adapter, or ``None`` when none is configured."""
        found = await self.llm()
        return self._factories.llm(found.connection) if found is not None else None

    # --- testing and saving ---------------------------------------------------------

    async def check(self, request: ConnectorInput) -> ConnectionCheck:
        """Test a connector without saving anything."""
        self._check_locks(request)
        adapter = await self._adapter_for(request)
        return await adapter.test()

    async def save(self, request: ConnectorInput) -> ConnectionCheck:
        """Test a connector and store it; nothing is written when the test fails."""
        self._check_locks(request)
        adapter = await self._adapter_for(request)
        check = await adapter.test()
        if not check.ok:
            raise problems.connector_failed(check.health)
        await self._store(request)
        logger.info("connector saved", extra={"connector": request.kind})
        return check

    async def list_models(self, request: ConnectorInput) -> list[str]:
        """List the models an AI provider offers, for the console's picker."""
        self._check_locks(request)
        provider = self._llm_adapter(await self._resolved_llm(request))
        return await provider.list_models()

    async def remove(self, kind: OptionalConnectorKind) -> None:
        """Forget a connector, in one write, leaving what the environment sets.

        A connector whose **key** an environment variable pins cannot be removed. Not
        out of tidiness: forgetting its address would leave it unconfigured, and an
        unconfigured connector with a pinned key is the one case where ``_secret_for``
        lets a new address through. Removal would otherwise be the way around the rule
        it enforces.

        A locked field that is neither the key nor part of the address — the seasons a
        series is requested with, say — stops nothing: the rest is forgotten and the
        environment keeps saying what it says.
        """
        fields = SETTING_FOR_FIELD[kind]
        if self._settings.is_locked(fields["api_key"]):
            raise _locked(fields["api_key"])
        removable = [
            setting for setting in fields.values() if not self._settings.is_locked(setting)
        ]
        await self._settings.delete_many(removable)
        logger.info("connector removed", extra={"connector": kind})

    # --- the rules ------------------------------------------------------------------

    def _check_locks(self, request: ConnectorInput) -> None:
        """Refuse a field the environment sets to another value (``setting_locked``)."""
        fields = SETTING_FOR_FIELD[request.kind]
        for name in request.given & set(fields):
            setting = fields[name]
            if not self._settings.is_locked(setting):
                continue
            if request.value_of(name) != self._settings.locked_value(setting):
                raise _locked(setting)

    def _effective(self, request: ConnectorInput, name: str, stored: object) -> object:
        """Return what a field is worth: environment, then request, then stored.

        A field the request did not carry keeps its stored value, which is what lets the
        console send "change the model" without resending the key — and which is exactly
        why the secret itself does **not** go through here.
        """
        setting = SETTING_FOR_FIELD[request.kind][name]
        if self._settings.is_locked(setting):
            return self._settings.locked_value(setting)
        given = request.value_of(name)
        return given if name in request.given and given is not None else stored

    def _locked_secret(self, request: ConnectorInput) -> str | None:
        setting = SETTING_FOR_FIELD[request.kind]["api_key"]
        value = self._settings.locked_value(setting)
        return value if self._settings.is_locked(setting) and isinstance(value, str) else None

    def _secret_for(
        self, request: ConnectorInput, stored: str | None, *, same_address: bool
    ) -> str:
        """Resolve the secret, and refuse to let it follow the connector elsewhere.

        This is the security model's section 7. A request that omits the key and moves
        the address gets ``secret_required``: the stored key is never sent anywhere it
        was not stored for.

        A key an environment variable pins obeys the **same** rule, and that is the part
        that is easy to get backwards. It is tempting to treat a pinned key as always
        available — it cannot be changed, so what is there to protect? — but a pinned key
        is a stored key, and an administrator cannot "send it again", because they are
        not allowed to set it at all. So once the connector has an address, the address
        is pinned with it, and moving it says which variable to change instead.
        Otherwise a stolen console session could point the connector at a collector and
        read the operator's key out of the request Tindarr obligingly sent.

        One case is left open: a connector nothing is stored for yet. The first address
        has to come from somewhere and there is none to protect; the save pins it.
        """
        locked = self._locked_secret(request)
        if locked is not None:
            if stored is not None and not same_address:
                raise SecretPinnedToItsAddressError(SETTING_FOR_FIELD[request.kind]["api_key"])
            return locked
        if request.api_key:
            return request.api_key
        if stored and same_address:
            return stored
        raise problems.secret_required()

    # --- resolving each kind --------------------------------------------------------

    async def _resolved_metadata(self, request: ConnectorInput) -> MetadataSettings:
        stored = await (self.tmdb() if request.kind == "tmdb" else self.omdb())
        # TMDb and OMDb each have exactly one host, written into their adapter: there is
        # no address an administrator could move the key to, so it is always reusable.
        return MetadataSettings(
            self._secret_for(request, stored.api_key if stored else None, same_address=True)
        )

    async def _resolved_requests(self, request: ConnectorInput) -> RequestsSettings:
        stored = await self.requests()
        url = self._effective(request, "url", stored.url if stored else None)
        verify_tls = self._effective(request, "verify_tls", stored.verify_tls if stored else True)
        seasons = self._effective(request, "tv_seasons", stored.tv_seasons if stored else "all")
        if not isinstance(url, str) or not url:
            raise _missing("url")
        # ``verify_tls`` belongs in the address: turning it off while omitting the key
        # would replay the stored key over a connection nobody checks, which is all an
        # on-path attacker needs.
        same = stored is not None and stored.url == url and stored.verify_tls == bool(verify_tls)
        return RequestsSettings(
            url=url,
            api_key=self._secret_for(
                request, stored.api_key if stored else None, same_address=same
            ),
            verify_tls=bool(verify_tls),
            tv_seasons="first" if seasons == "first" else "all",
        )

    async def _resolved_llm(self, request: ConnectorInput) -> LlmSettings:
        stored = await self.llm()
        provider = _as_provider(
            self._effective(request, "provider", stored.provider if stored else None)
        )
        if provider is None:
            raise _missing("provider")
        base_url = self._effective(request, "base_url", stored.base_url if stored else None)
        model = self._effective(request, "model", stored.model if stored else None)
        effort = self._effective(
            request, "reasoning_effort", stored.reasoning_effort if stored else None
        )
        # The provider decides the address as surely as the address does: switching from
        # a local gateway to a hosted one must not carry the old key along.
        same = (
            stored is not None
            and stored.provider == provider
            and stored.base_url == (base_url if isinstance(base_url, str) else None)
        )
        key = self._llm_key(request, stored, provider, same=same)
        return LlmSettings(
            provider=provider,
            api_key=key,
            base_url=base_url if isinstance(base_url, str) and base_url else None,
            model=model if isinstance(model, str) else "",
            reasoning_effort=_as_effort(effort),
        )

    def _llm_key(
        self,
        request: ConnectorInput,
        stored: LlmSettings | None,
        provider: LlmProviderKind,
        *,
        same: bool,
    ) -> str:
        if provider in _KEYLESS_PROVIDERS:
            # Ollama has no accounts, and a local gateway usually has none either, so
            # this connector saves with no key at all. A key that *was* given still
            # belongs to the address it was given for: moving the address asks for it
            # again rather than sending it somewhere new. A key an environment variable
            # pins obeys the same rule, and says which variable to change instead.
            locked = self._locked_secret(request)
            if locked is not None:
                if stored is not None and not same:
                    raise SecretPinnedToItsAddressError(SETTING_FOR_FIELD[request.kind]["api_key"])
                return locked
            if request.api_key:
                return request.api_key
            held = stored.api_key if stored else None
            if held and not same:
                raise problems.secret_required()
            return held if held and same else ""
        return self._secret_for(request, stored.api_key if stored else None, same_address=same)

    # --- building the adapter a request describes ------------------------------------

    async def _adapter_for(self, request: ConnectorInput) -> "_Testable":
        if request.kind == "tmdb":
            return self._factories.metadata((await self._resolved_metadata(request)).api_key)
        if request.kind == "omdb":
            return self._factories.ratings((await self._resolved_metadata(request)).api_key)
        if request.kind == "requests":
            found = await self._resolved_requests(request)
            return self._factories.request_backend(
                RequestBackendConnection(
                    url=found.url,
                    api_key=found.api_key,
                    media_server_kind=await self.media_server_kind(),
                    tv_seasons=found.tv_seasons,
                    verify_tls=found.verify_tls,
                )
            )
        return self._llm_adapter(await self._resolved_llm(request))

    def _llm_adapter(self, settings: LlmSettings) -> LlmProvider:
        return self._factories.llm(settings.connection)

    # --- writing ---------------------------------------------------------------------

    async def _store(self, request: ConnectorInput) -> None:
        values = await self._values_to_store(request)
        unlocked = {
            name: value for name, value in values.items() if not self._settings.is_locked(name)
        }
        await self._settings.set_many(unlocked)

    async def _values_to_store(self, request: ConnectorInput) -> dict[str, object]:
        if request.kind in ("tmdb", "omdb"):
            found = await self._resolved_metadata(request)
            return {SETTING_FOR_FIELD[request.kind]["api_key"]: found.api_key}
        if request.kind == "requests":
            backend = await self._resolved_requests(request)
            return {
                "requests_url": backend.url,
                "requests_api_key": backend.api_key,
                "requests_verify_tls": backend.verify_tls,
                "requests_tv_seasons": backend.tv_seasons,
            }
        provider = await self._resolved_llm(request)
        return {
            "llm_provider": provider.provider,
            "llm_api_key": provider.api_key,
            "llm_base_url": provider.base_url,
            "llm_model": provider.model,
            "llm_reasoning_effort": provider.reasoning_effort,
        }


class _Testable(Protocol):
    """Anything with a connection test; every adapter in this package has one."""

    async def test(self) -> ConnectionCheck:
        """Check the connector, without raising for a remote failure."""
        ...


def _as_provider(value: object) -> LlmProviderKind | None:
    """Return a stored or given value as an AI provider kind, or ``None``."""
    return next((kind for kind in LLM_PROVIDER_KINDS if kind == value), None)


def _as_effort(value: object) -> ReasoningEffort | None:
    """Return a stored or given value as a reasoning effort, or ``None``."""
    return next((effort for effort in REASONING_EFFORTS if effort == value), None)


def _locked(setting: str) -> ProblemError:
    return SettingLockedError(setting)


def _missing(field_name: str) -> ProblemError:
    return ProblemError(
        HTTPStatus.BAD_REQUEST, "validation_error", f"{field_name}: this connector needs it"
    )
