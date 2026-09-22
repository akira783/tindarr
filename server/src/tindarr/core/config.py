"""Bootstrap configuration, read from the environment before the database is opened.

Every value comes from ``TINDARR_<NAME>`` or, for Docker secrets, from the file named
by ``TINDARR_<NAME>_FILE``. When both are set, the file wins: it is the more deliberate
source, and it keeps a stray environment variable from silently replacing a secret.
An empty value counts as unset.

Only what is needed before the database can be read lives here (data directory, secret
key, listening address, logging, proxies). Everything else is a database setting, which
an environment variable can still override and lock (see ``tindarr.storage.settings``).
"""

import os
from collections.abc import Mapping
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from tindarr.core.net import parse_host

ENV_PREFIX = "TINDARR_"
FILE_SUFFIX = "_FILE"
DEFAULT_PORT = 8787
#: Where the runtime image copies the built web console (docs/architecture.md).
DEFAULT_WEB_DIR = Path("/app/web")

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class ConfigError(Exception):
    """The environment does not describe a valid configuration.

    Messages never contain the offending value, which may be a secret.
    """


def env_var_name(name: str) -> str:
    """Return the environment variable that holds setting ``name``."""
    return ENV_PREFIX + name.upper()


def read_env(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """Return the value of ``TINDARR_<NAME>``, or the content of ``TINDARR_<NAME>_FILE``.

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


class ServerConfig(BaseModel):
    """Configuration needed to start the server.

    Building it directly reads nothing from the environment; ``load_config`` does.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

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
    hsts: bool = Field(
        default=False,
        description="Send Strict-Transport-Security (only when always reached over HTTPS).",
    )
    trusted_proxies: tuple[IPv4Network | IPv6Network, ...] = Field(
        default=(),
        description="Comma-separated IPs or CIDRs whose X-Forwarded-* headers are honoured.",
    )
    allowed_hosts: tuple[str, ...] = Field(
        default=(),
        description="Comma-separated host names accepted in the Host header, besides IP "
        "literals, localhost and the host of public_url.",
    )
    allow_http_console: bool = Field(
        default=False,
        description="Allow console sessions over plain HTTP from private client addresses.",
    )
    db_backups_keep: int = Field(
        default=5, ge=1, le=100, description="Pre-migration database backups to keep."
    )
    web_dir: Path = Field(
        default=DEFAULT_WEB_DIR,
        description="Directory holding the built web console, served under /.",
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _parse_allowed_hosts(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        hosts: list[str] = []
        for item in value.split(","):
            name = item.strip()
            if not name:
                continue
            host = parse_host(name)
            if host is None or host.port is not None:
                msg = "must be a comma-separated list of host names, without port or scheme"
                raise ValueError(msg)
            hosts.append(host.host)
        return tuple(hosts)

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
    values: dict[str, Any] = {}
    for name in ServerConfig.model_fields:
        value = read_env(name)
        if value is not None:
            values[name] = value
    try:
        return ServerConfig.model_validate(values | overrides)
    except ValidationError as exc:
        lines = [
            f"{env_var_name('.'.join(str(part) for part in error['loc']))}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
        raise ConfigError("invalid configuration: " + "; ".join(lines)) from None
