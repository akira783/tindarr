from ipaddress import ip_network
from pathlib import Path

import pytest

from tindarr.core.config import ConfigError, ServerConfig, load_config, read_env


def test_defaults() -> None:
    config = load_config()
    assert config.data_dir == Path("data")
    assert config.secret_key is None
    assert config.host == "127.0.0.1"
    assert config.port == 8787
    assert config.log_level == "INFO"
    assert config.api_docs is False
    assert config.trusted_proxies == ()
    assert config.db_backups_keep == 5


def test_reads_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINDARR_PORT", "9000")
    monkeypatch.setenv("TINDARR_API_DOCS", "true")
    monkeypatch.setenv("TINDARR_LOG_LEVEL", "debug")
    monkeypatch.setenv("TINDARR_DATA_DIR", "/srv/tindarr")
    config = load_config()
    assert config.port == 9000
    assert config.api_docs is True
    assert config.log_level == "DEBUG"
    assert config.data_dir == Path("/srv/tindarr")


def test_unprefixed_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "9000")
    assert load_config().port == 8787


def test_file_variant_is_read_without_trailing_newline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret_file = tmp_path / "key"
    secret_file.write_text("s" * 40 + "\n")
    monkeypatch.setenv("TINDARR_SECRET_KEY_FILE", str(secret_file))
    config = load_config()
    assert config.secret_key is not None
    assert config.secret_key.get_secret_value() == "s" * 40
    assert "s" * 40 not in repr(config)


def test_file_wins_over_plain_variable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    port_file = tmp_path / "port"
    port_file.write_text("9001")
    monkeypatch.setenv("TINDARR_PORT", "9000")
    monkeypatch.setenv("TINDARR_PORT_FILE", str(port_file))
    assert load_config().port == 9001


def test_empty_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINDARR_SECRET_KEY", "")
    monkeypatch.setenv("TINDARR_PORT_FILE", "")
    config = load_config()
    assert config.secret_key is None
    assert config.port == 8787


def test_unreadable_file_is_a_config_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TINDARR_SECRET_KEY_FILE", str(tmp_path / "missing"))
    with pytest.raises(ConfigError, match="TINDARR_SECRET_KEY_FILE: cannot read"):
        load_config()


def test_read_env_accepts_an_explicit_mapping() -> None:
    assert read_env("server_name", {"TINDARR_SERVER_NAME": "Home"}) == "Home"
    assert read_env("server_name", {}) is None


def test_invalid_values_never_echo_the_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINDARR_PORT", "hunter2-not-a-port")
    with pytest.raises(ConfigError) as caught:
        load_config()
    assert "TINDARR_PORT" in str(caught.value)
    assert "hunter2" not in str(caught.value)


def test_unknown_keyword_is_rejected() -> None:
    with pytest.raises(ConfigError, match="TINDARR_NOPE"):
        load_config(nope=1)


def test_trusted_proxies_accept_addresses_and_networks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINDARR_TRUSTED_PROXIES", "10.0.0.1, 172.16.0.0/12,,fd00::/8")
    assert load_config().trusted_proxies == (
        ip_network("10.0.0.1/32"),
        ip_network("172.16.0.0/12"),
        ip_network("fd00::/8"),
    )


def test_trusted_proxies_reject_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINDARR_TRUSTED_PROXIES", "10.0.0.1,proxy.lan")
    with pytest.raises(ConfigError, match="TINDARR_TRUSTED_PROXIES"):
        load_config()


def test_config_is_immutable() -> None:
    config = ServerConfig()
    with pytest.raises(ValueError, match="frozen"):
        config.port = 1  # pyright: ignore[reportAttributeAccessIssue]


def test_explicit_overrides_win_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINDARR_PORT", "9000")
    monkeypatch.setenv("TINDARR_HOST", "0.0.0.0")  # noqa: S104 - never bound here
    config = load_config(port=9100)
    assert (config.port, config.host) == (9100, "0.0.0.0")  # noqa: S104


def test_building_the_model_directly_reads_no_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TINDARR_PORT", "9000")
    assert ServerConfig().port == 8787


def test_hsts_is_read_as_a_boolean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINDARR_HSTS", "false")
    assert load_config().hsts is False
    monkeypatch.setenv("TINDARR_HSTS", "true")
    assert load_config().hsts is True


def test_allowed_hosts_are_parsed_and_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINDARR_ALLOWED_HOSTS", "Tindarr.Example.com, console.lan ,")
    assert load_config().allowed_hosts == ("tindarr.example.com", "console.lan")


@pytest.mark.parametrize("value", ["console.lan:8787", "https://console.lan", "not a host"])
def test_invalid_allowed_hosts_are_refused(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINDARR_ALLOWED_HOSTS", value)
    with pytest.raises(ConfigError, match="TINDARR_ALLOWED_HOSTS"):
        load_config()


def test_the_plain_http_console_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_config().allow_http_console is False
    monkeypatch.setenv("TINDARR_ALLOW_HTTP_CONSOLE", "true")
    assert load_config().allow_http_console is True
