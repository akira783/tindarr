"""Settings stored in the database, with secret values encrypted.

Each known setting is declared in ``SETTINGS``. Its value is JSON, stored as text. A
secret setting is encrypted with AES-256-GCM, bound to its name (see
``tindeerr.core.crypto``). Any setting can be forced by ``TINDEERR_<NAME>`` (or
``_FILE``): the environment value then wins and the setting is locked against changes
from the app.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from tindeerr.core.config import read_env
from tindeerr.core.crypto import SecretCipher
from tindeerr.core.logs import register_secret
from tindeerr.storage.db import write_transaction
from tindeerr.storage.tables import settings as settings_table

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
type SettingSource = Literal["default", "database", "environment"]


@dataclass(frozen=True)
class SettingDefinition:
    """A setting the server knows about."""

    name: str
    secret: bool = False
    default: JsonValue = None


SETTINGS: Final[Mapping[str, SettingDefinition]] = {
    definition.name: definition
    for definition in (
        SettingDefinition("server_name", default="Tindeerr"),
        SettingDefinition("media_server_kind"),
        SettingDefinition("media_server_url"),
        SettingDefinition("media_server_api_key", secret=True),
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


class SettingLockedError(Exception):
    """The setting is set by an environment variable (problem ``code`` ``setting_locked``)."""


def environment_overrides(
    definitions: Mapping[str, SettingDefinition] = SETTINGS,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Read ``TINDEERR_<NAME>`` / ``_FILE`` for every declared setting."""
    overrides: dict[str, str] = {}
    for name in definitions:
        value = read_env(name, environ)
        if value is not None:
            overrides[name] = value
    return overrides


class SettingsStore:
    """Reads and writes settings; environment overrides take precedence."""

    def __init__(
        self,
        engine: AsyncEngine,
        cipher: SecretCipher,
        overrides: Mapping[str, str],
        definitions: Mapping[str, SettingDefinition] = SETTINGS,
    ) -> None:
        unknown = set(overrides) - set(definitions)
        if unknown:
            msg = f"overrides for undeclared settings: {sorted(unknown)}"
            raise UnknownSettingError(msg)
        self._engine = engine
        self._cipher = cipher
        self._definitions = definitions
        self._overrides = dict(overrides)
        for name, value in self._overrides.items():
            if definitions[name].secret:
                register_secret(value)

    def _definition(self, name: str) -> SettingDefinition:
        try:
            return self._definitions[name]
        except KeyError:
            raise UnknownSettingError(name) from None

    def is_locked(self, name: str) -> bool:
        """Whether an environment variable sets ``name``."""
        self._definition(name)
        return name in self._overrides

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
            value: JsonValue = json.loads(plaintext)
            if isinstance(value, str):
                register_secret(value)
        else:
            value = json.loads(stored)
        return SettingValue(value, "database")

    async def set(self, name: str, value: JsonValue) -> None:
        """Store ``value``. Raises ``SettingLockedError`` for an environment-set setting."""
        definition = self._definition(name)
        if name in self._overrides:
            raise SettingLockedError(name)
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if definition.secret:
            if isinstance(value, str):
                register_secret(value)
            serialized = self._cipher.encrypt(serialized, context=self._context(name))
        row = {
            "name": name,
            "value": serialized,
            "encrypted": definition.secret,
            "updated_at": datetime.now(UTC),
        }
        statement = insert(settings_table).values(row)
        statement = statement.on_conflict_do_update(
            index_elements=[settings_table.c.name],
            set_={key: statement.excluded[key] for key in ("value", "encrypted", "updated_at")},
        )
        async with write_transaction(self._engine) as connection:
            await connection.execute(statement)

    async def delete(self, name: str) -> None:
        """Remove the stored value, falling back to the default."""
        self._definition(name)
        if name in self._overrides:
            raise SettingLockedError(name)
        async with write_transaction(self._engine) as connection:
            await connection.execute(delete(settings_table).where(settings_table.c.name == name))

    @staticmethod
    def _context(name: str) -> str:
        return f"setting:{name}"
