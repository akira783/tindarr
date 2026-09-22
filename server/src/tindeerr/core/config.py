"""Bootstrap configuration, read from the environment before the database is opened.

Every value comes from ``TINDEERR_<NAME>`` or, for Docker secrets, from the file named
by ``TINDEERR_<NAME>_FILE``. When both are set, the file wins: it is the more deliberate
source, and it keeps a stray environment variable from silently replacing a secret.
An empty value counts as unset.

Only what is needed before the database can be read lives here (data directory, secret
key, listening address, logging, proxies). Everything else is a database setting, which
an environment variable can still override and lock (see ``tindeerr.storage.settings``).
"""

import os
from collections.abc import Mapping
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path
from typing import Any, Literal, override

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

ENV_PREFIX = "TINDEERR_"
FILE_SUFFIX = "_FILE"
DEFAULT_PORT = 8787

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class ConfigError(Exception):
    """The environment does not describe a valid configuration.

    Messages never contain the offending value, which may be a secret.
    """


def env_var_name(name: str) -> str:
    """Return the environment variable that holds setting ``name``."""
    return ENV_PREFIX + name.upper()


def read_env(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """Return the value of ``TINDEERR_<NAME>``, or the content of ``TINDEERR_<NAME>_FILE``.

    The file wins when both are set. One trailing newline sequence is removed from file
    content, since editors and ``echo`` add one. Empty values are treated as unset.
    """
    env = os.environ if environ is None else environ
    key = env_var_name(name)
    file_path = env.get(key + FILE_SUFFIX)
    if file_path:
        try:
            content = Path(file_path).read_text(encoding="utf-8")
        except OSError as exc:
            msg = f"{key}{FILE_SUFFIX}: cannot read {file_path} ({exc.strerror})"
            raise ConfigError(msg) from None
        value = content.removesuffix("\n").removesuffix("\r")
    else:
        value = env.get(key)
    return value or None


class _EnvironmentSource(PydanticBaseSettingsSource):
    """Settings source implementing the ``TINDEERR_<NAME>`` / ``_FILE`` rule."""

    @override
    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return read_env(field_name), field_name, False

    @override
    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, field in self.settings_cls.model_fields.items():
            value, key, _ = self.get_field_value(field, name)
            if value is not None:
                values[key] = value
        return values


class ServerConfig(BaseSettings):
    """Configuration needed to start the server."""

    model_config = SettingsConfigDict(frozen=True, extra="forbid")

    data_dir: Path = Field(
        default=Path("data"), description="Directory holding the database, backups and key."
    )
    secret_key: SecretStr | None = Field(
        default=None,
        description="Master key material. Generated into <data_dir>/secret.key when unset.",
    )
    host: str = Field(default="127.0.0.1", description="Listening address.")
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535, description="Listening port.")
    log_level: LogLevel = Field(default="INFO", description="Minimum log level.")
    api_docs: bool = Field(default=False, description="Serve the interactive API docs.")
    trusted_proxies: tuple[IPv4Network | IPv6Network, ...] = Field(
        default=(),
        description="Comma-separated IPs or CIDRs whose X-Forwarded-* headers are honoured.",
    )
    db_backups_keep: int = Field(
        default=5, ge=1, le=100, description="Pre-migration database backups to keep."
    )

    @override
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Explicit arguments first (tests, embedding), then the environment. No .env files.
        return (init_settings, _EnvironmentSource(settings_cls))

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _parse_trusted_proxies(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        items = [item.strip() for item in value.split(",") if item.strip()]
        try:
            return tuple(ip_network(item, strict=False) for item in items)
        except ValueError:
            msg = "must be a comma-separated list of IP addresses or CIDR networks"
            raise ValueError(msg) from None


def load_config(**overrides: Any) -> ServerConfig:
    """Build the configuration from the environment, plus explicit ``overrides``.

    Raises ``ConfigError`` with one line per invalid field and never echoes the input.
    """
    try:
        return ServerConfig(**overrides)
    except ValidationError as exc:
        lines = [
            f"{env_var_name('.'.join(str(part) for part in error['loc']))}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
        raise ConfigError("invalid configuration: " + "; ".join(lines)) from None
