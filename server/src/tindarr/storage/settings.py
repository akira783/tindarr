"""Settings stored in the database, with secret values encrypted.

Each known setting is declared in ``SETTINGS`` with a type. Its value is JSON, stored as
text. A secret setting is encrypted with AES-256-GCM, bound to its name (see
``tindarr.core.crypto``). Any setting can be forced by ``TINDARR_<NAME>`` (or
``_FILE``): the environment value then wins and the setting is locked against changes
from the app.

Values are validated against the setting's type everywhere they enter: environment
values are parsed at startup (``TINDARR_EXCLUDE_ADULT=false`` is ``False``, not a
truthy string; an invalid one stops the server with a message naming the variable),
``set`` rejects a wrong value with a ``validation_error`` problem, and a stored value
that no longer fits its type falls back to the default with a warning.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property
from http import HTTPStatus
from typing import Annotated, Any, Final, Literal, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tindarr.core.config import ConfigError, env_var_name, read_env
from tindarr.core.crypto import SecretCipher
from tindarr.core.errors import ProblemError
from tindarr.core.logs import register_secret
from tindarr.core.net import normalize_public_url
from tindarr.ports.llm import LlmProviderKind, ReasoningEffort
from tindarr.ports.media_server import MediaServerKind
from tindarr.ports.request_backend import SeasonPolicy
from tindarr.storage.db import write_transaction
from tindarr.storage.tables import settings as settings_table

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
type SettingSource = Literal["default", "database", "environment"]
#: Whether Jellyfin / Emby password sign-in is offered, and from where.
type PasswordSignIn = Literal["enabled", "lan_only", "disabled"]
#: An origin the phones reach this server at (docs/auth.md, section 10).
type PublicUrl = Annotated[str, AfterValidator(normalize_public_url)]
type StreamingRegion = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]


def as_password_sign_in(value: object) -> PasswordSignIn:
    """Return a stored ``password_sign_in`` value as its type, defaulting to ``enabled``."""
    if value in ("enabled", "lan_only", "disabled"):
        return value
    return "enabled"


class ContentFilters(BaseModel):
    """Titles the swipe engine must never offer (stored in step 2, used from step 4)."""

    model_config = ConfigDict(extra="forbid")

    exclude_adult: bool = True
    min_year: int | None = None
    excluded_genres: list[str] = Field(default_factory=list)
    excluded_original_languages: list[str] = Field(default_factory=list)


logger = logging.getLogger(__name__)


def _error_messages(error: ValidationError) -> str:
    # Messages only: the rejected value may be a secret.
    return "; ".join(
        str(item["msg"]) for item in error.errors(include_input=False, include_url=False)
    )


@dataclass(frozen=True)
class SettingDefinition:
    """A setting the server knows about.

    ``value_type`` is any type pydantic can validate whose values serialise to JSON
    (``str | None``, ``bool``, a ``Literal``, ``list[str]``...).
    """

    name: str
    value_type: Any = field(repr=False)
    secret: bool = False
    default: JsonValue = None

    @cached_property
    def _adapter(self) -> TypeAdapter[Any]:
        return TypeAdapter(self.value_type)

    def validate(self, value: object) -> JsonValue:
        """Return ``value`` checked against the type, as JSON data.

        Raises ``pydantic.ValidationError``.
        """
        return self._to_json(self._adapter.validate_python(value))

    def parse(self, raw: str) -> JsonValue:
        """Parse an environment string: scalars as text (``false``, ``42``), else JSON.

        Raises ``pydantic.ValidationError``.
        """
        try:
            return self._to_json(self._adapter.validate_strings(raw))
        except ValidationError:
            if not raw.lstrip().startswith(("[", "{")):
                raise
        return self._to_json(self._adapter.validate_json(raw))

    def _to_json(self, value: object) -> JsonValue:
        return cast("JsonValue", self._adapter.dump_python(value, mode="json"))


SETTINGS: Final[Mapping[str, SettingDefinition]] = {
    definition.name: definition
    for definition in (
        SettingDefinition("server_name", str, default="Tindarr"),
        SettingDefinition("media_server_kind", MediaServerKind | None),
        SettingDefinition("media_server_url", str | None),
        SettingDefinition("media_server_api_key", str | None, secret=True),
        # What the media server calls itself, read at the last successful test. Shown
        # by the wizard and by ``server/info``; never set by hand.
        SettingDefinition("media_server_name", str | None),
        SettingDefinition("media_server_verify_tls", bool, default=True),
        SettingDefinition("public_url", PublicUrl | None),
        SettingDefinition("password_sign_in", PasswordSignIn, default="enabled"),
        # The optional connectors (step 3). Each is absent until an administrator
        # configures it, and each secret is encrypted like the media server's.
        SettingDefinition("tmdb_api_key", str | None, secret=True),
        SettingDefinition("omdb_api_key", str | None, secret=True),
        SettingDefinition("requests_url", str | None),
        SettingDefinition("requests_api_key", str | None, secret=True),
        SettingDefinition("requests_verify_tls", bool, default=True),
        SettingDefinition("requests_tv_seasons", SeasonPolicy, default="all"),
        SettingDefinition("llm_provider", LlmProviderKind | None),
        SettingDefinition("llm_api_key", str | None, secret=True),
        SettingDefinition("llm_base_url", str | None),
        SettingDefinition("llm_model", str | None),
        SettingDefinition("llm_reasoning_effort", ReasoningEffort | None),
        # Stored from step 2, used by the swipe engine from step 4.
        SettingDefinition("language", str, default="en"),
        SettingDefinition("streaming_region", StreamingRegion | None),
        SettingDefinition("daily_generation_limit", Annotated[int, Field(ge=0)], default=10),
        SettingDefinition("warm_up_enabled", bool, default=True),
        SettingDefinition(
            "content_filters",
            ContentFilters,
            default=cast("JsonValue", ContentFilters().model_dump(mode="json")),
        ),
    )
}


@dataclass(frozen=True)
class SettingValue:
    """A setting's effective value and where it comes from."""

    value: JsonValue
    source: SettingSource

    @property
    def locked(self) -> bool:
        """Set by the environment, so it cannot be changed from the app."""
        return self.source == "environment"


class UnknownSettingError(LookupError):
    """No setting with that name is declared."""


class InvalidSettingValueError(ProblemError):
    """The value does not fit the setting's type (400, ``validation_error``)."""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(HTTPStatus.BAD_REQUEST, "validation_error", f"{name}: {reason}")
        self.name = name


class SettingLockedError(ProblemError):
    """The setting is set by an environment variable (409, ``setting_locked``)."""

    def __init__(self, name: str) -> None:
        super().__init__(
            HTTPStatus.CONFLICT,
            "setting_locked",
            f"{name} is set by {env_var_name(name)} and cannot be changed here",
        )
        self.name = name


def environment_overrides(
    definitions: Mapping[str, SettingDefinition] = SETTINGS,
    environ: Mapping[str, str] | None = None,
) -> dict[str, JsonValue]:
    """Read and validate ``TINDARR_<NAME>`` / ``_FILE`` for every declared setting.

    Raises ``ConfigError`` naming each invalid variable, never echoing its value.
    """
    overrides: dict[str, JsonValue] = {}
    problems: list[str] = []
    for name, definition in definitions.items():
        raw = read_env(name, environ)
        if raw is None:
            continue
        try:
            overrides[name] = definition.parse(raw)
        except ValidationError as exc:
            problems.append(f"{env_var_name(name)}: {_error_messages(exc)}")
    if problems:
        raise ConfigError("invalid setting override: " + "; ".join(problems))
    return overrides


class SettingsStore:
    """Reads and writes settings; environment overrides take precedence."""

    def __init__(
        self,
        engine: AsyncEngine,
        cipher: SecretCipher,
        overrides: Mapping[str, object],
        definitions: Mapping[str, SettingDefinition] = SETTINGS,
    ) -> None:
        unknown = set(overrides) - set(definitions)
        if unknown:
            msg = f"overrides for undeclared settings: {sorted(unknown)}"
            raise UnknownSettingError(msg)
        self._engine = engine
        self._cipher = cipher
        self._definitions = definitions
        self._overrides = {
            name: self._validated(definitions[name], value) for name, value in overrides.items()
        }
        for name, value in self._overrides.items():
            if definitions[name].secret and isinstance(value, str):
                register_secret(value)

    @staticmethod
    def _validated(definition: SettingDefinition, value: object) -> JsonValue:
        try:
            return definition.validate(value)
        except ValidationError as exc:
            raise InvalidSettingValueError(definition.name, _error_messages(exc)) from None

    def _definition(self, name: str) -> SettingDefinition:
        try:
            return self._definitions[name]
        except KeyError:
            raise UnknownSettingError(name) from None

    def is_locked(self, name: str) -> bool:
        """Whether an environment variable sets ``name``."""
        self._definition(name)
        return name in self._overrides

    def locked_value(self, name: str) -> JsonValue:
        """Return the value an environment variable forces on ``name`` (else ``None``)."""
        self._definition(name)
        return self._overrides.get(name)

    async def get(self, name: str) -> SettingValue:
        """Return the effective value: environment, then database, then default."""
        definition = self._definition(name)
        if name in self._overrides:
            return SettingValue(self._overrides[name], "environment")
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(settings_table.c.value, settings_table.c.encrypted).where(
                        settings_table.c.name == name
                    )
                )
            ).one_or_none()
        if row is None:
            return SettingValue(definition.default, "default")
        stored, encrypted = row.value, row.encrypted
        if encrypted:
            plaintext = self._cipher.decrypt(stored, context=self._context(name))
            value: object = json.loads(plaintext)
            if isinstance(value, str):
                register_secret(value)
        else:
            value = json.loads(stored)
        try:
            return SettingValue(definition.validate(value), "database")
        except ValidationError:
            logger.warning(
                "stored setting does not fit its type; using the default",
                extra={"setting": name},
            )
            return SettingValue(definition.default, "default")

    async def set(self, name: str, value: object) -> None:
        """Validate and store ``value``.

        Raises ``InvalidSettingValueError`` for a value of the wrong type and
        ``SettingLockedError`` for an environment-set setting.
        """
        statement = insert(settings_table).values(self._row(name, value))
        statement = statement.on_conflict_do_update(
            index_elements=[settings_table.c.name],
            set_={key: statement.excluded[key] for key in ("value", "encrypted", "updated_at")},
        )
        async with write_transaction(self._engine) as connection:
            await connection.execute(statement)

    async def set_many(
        self, values: Mapping[str, object], connection: AsyncConnection | None = None
    ) -> None:
        """Validate and store several settings, all or nothing.

        ``connection`` runs them inside a transaction the caller already opened, so a
        write that belongs with them (the media server's identity) lands with them.
        """
        rows = [self._row(name, value) for name, value in values.items()]
        if connection is not None:
            await self._write(connection, rows)
            return
        async with write_transaction(self._engine) as owned:
            await self._write(owned, rows)

    @staticmethod
    async def _write(connection: AsyncConnection, rows: list[dict[str, object]]) -> None:
        for row in rows:
            statement = insert(settings_table).values(row)
            await connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[settings_table.c.name],
                    set_={
                        key: statement.excluded[key] for key in ("value", "encrypted", "updated_at")
                    },
                )
            )

    async def delete(self, name: str) -> None:
        """Remove the stored value, falling back to the default."""
        self._definition(name)
        if name in self._overrides:
            raise SettingLockedError(name)
        async with write_transaction(self._engine) as connection:
            await connection.execute(delete(settings_table).where(settings_table.c.name == name))

    def _row(self, name: str, value: object) -> dict[str, object]:
        """Return the row to store for ``name``, encrypting a secret setting."""
        definition = self._definition(name)
        if name in self._overrides:
            raise SettingLockedError(name)
        value = self._validated(definition, value)
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if definition.secret:
            if isinstance(value, str):
                register_secret(value)
            serialized = self._cipher.encrypt(serialized, context=self._context(name))
        return {
            "name": name,
            "value": serialized,
            "encrypted": definition.secret,
            "updated_at": datetime.now(UTC),
        }

    @staticmethod
    def _context(name: str) -> str:
        return f"setting:{name}"
