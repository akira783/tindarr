import os
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from tindeerr.core.config import ConfigError, ServerConfig
from tindeerr.core.crypto import DecryptionError, SecretCipher
from tindeerr.core.logs import REDACTED, redact_text
from tindeerr.storage.settings import (
    SETTINGS,
    InvalidSettingValueError,
    JsonValue,
    SettingDefinition,
    SettingLockedError,
    SettingsStore,
    UnknownSettingError,
    environment_overrides,
)
from tindeerr.storage.tables import settings as settings_table

pytestmark = pytest.mark.anyio

CIPHER = SecretCipher(os.urandom(32), "0a1b2c3d")
DEFINITIONS = {
    **SETTINGS,
    "other_api_key": SettingDefinition("other_api_key", str | None, secret=True),
    "filters": SettingDefinition(
        "filters", dict[str, bool | int | list[str]], default={"exclude_adult": True}
    ),
    "exclude_adult": SettingDefinition("exclude_adult", bool, default=True),
    "daily_cap": SettingDefinition("daily_cap", int, default=20),
    "genres": SettingDefinition("genres", list[str], default=[]),
}


def make_store(engine: AsyncEngine, overrides: dict[str, object] | None = None) -> SettingsStore:
    return SettingsStore(engine, CIPHER, overrides or {}, DEFINITIONS)


async def raw_value(engine: AsyncEngine, name: str) -> str:
    async with engine.connect() as connection:
        value = await connection.scalar(
            select(settings_table.c.value).where(settings_table.c.name == name)
        )
    assert isinstance(value, str)
    return value


async def test_unset_setting_returns_its_default(engine: AsyncEngine) -> None:
    store = make_store(engine)
    name = await store.get("server_name")
    assert (name.value, name.source, name.locked) == ("Tindeerr", "default", False)
    assert (await store.get("media_server_kind")).value is None
    assert (await store.get("filters")).value == {"exclude_adult": True}


async def test_plain_value_round_trip_and_update(engine: AsyncEngine) -> None:
    store = make_store(engine)
    await store.set("server_name", "Home")
    await store.set("server_name", "Chez nous")
    value = await store.get("server_name")
    assert (value.value, value.source) == ("Chez nous", "database")
    assert await raw_value(engine, "server_name") == '"Chez nous"'


async def test_json_values_are_preserved(engine: AsyncEngine) -> None:
    store = make_store(engine)
    filters: JsonValue = {"exclude_adult": False, "min_year": 1990, "excluded_genres": ["Horror"]}
    await store.set("filters", filters)
    assert (await store.get("filters")).value == filters


async def test_secret_is_encrypted_at_rest(engine: AsyncEngine) -> None:
    store = make_store(engine)
    await store.set("media_server_api_key", "jf-api-key-0123456789")
    raw = await raw_value(engine, "media_server_api_key")
    assert raw.startswith("v1:0a1b2c3d:")
    assert "jf-api-key" not in raw
    assert (await store.get("media_server_api_key")).value == "jf-api-key-0123456789"


async def test_secret_values_are_redacted_from_logs(engine: AsyncEngine) -> None:
    store = make_store(engine)
    await store.set("media_server_api_key", "jf-api-key-0123456789")
    assert redact_text("key jf-api-key-0123456789 rejected") == f"key {REDACTED} rejected"


async def test_tampered_secret_is_detected(engine: AsyncEngine) -> None:
    store = make_store(engine)
    await store.set("media_server_api_key", "jf-api-key-0123456789")
    raw = await raw_value(engine, "media_server_api_key")
    flipped = raw[:-2] + ("A" if raw[-2] != "A" else "B") + raw[-1]
    async with engine.begin() as connection:
        await connection.execute(
            update(settings_table)
            .where(settings_table.c.name == "media_server_api_key")
            .values(value=flipped)
        )
    with pytest.raises(DecryptionError):
        await store.get("media_server_api_key")


async def test_secret_moved_to_another_setting_does_not_decrypt(engine: AsyncEngine) -> None:
    store = make_store(engine)
    await store.set("media_server_api_key", "jf-api-key-0123456789")
    await store.set("other_api_key", "placeholder-value")
    stolen = await raw_value(engine, "media_server_api_key")
    async with engine.begin() as connection:
        await connection.execute(
            update(settings_table)
            .where(settings_table.c.name == "other_api_key")
            .values(value=stolen)
        )
    with pytest.raises(DecryptionError, match="authentication"):
        await store.get("other_api_key")


async def test_environment_override_wins_and_locks(engine: AsyncEngine) -> None:
    store = make_store(engine)
    await store.set("server_name", "From the app")
    locked = make_store(engine, {"server_name": "From env"})
    value = await locked.get("server_name")
    assert (value.value, value.source, value.locked) == ("From env", "environment", True)
    assert locked.is_locked("server_name")
    assert not locked.is_locked("media_server_url")
    with pytest.raises(SettingLockedError):
        await locked.set("server_name", "Other")
    with pytest.raises(SettingLockedError):
        await locked.delete("server_name")
    assert await raw_value(engine, "server_name") == '"From the app"'


async def test_secret_override_is_registered_for_redaction(engine: AsyncEngine) -> None:
    make_store(engine, {"media_server_api_key": "env-secret-9876543210"})
    assert "env-secret" not in redact_text("using env-secret-9876543210")


async def test_delete_falls_back_to_default(engine: AsyncEngine) -> None:
    store = make_store(engine)
    await store.set("server_name", "Home")
    await store.delete("server_name")
    assert (await store.get("server_name")).source == "default"


async def test_unknown_settings_are_rejected(engine: AsyncEngine) -> None:
    store = make_store(engine)
    with pytest.raises(UnknownSettingError):
        await store.get("nope")
    with pytest.raises(UnknownSettingError):
        await store.set("nope", 1)
    with pytest.raises(UnknownSettingError):
        store.is_locked("nope")
    with pytest.raises(UnknownSettingError):
        make_store(engine, {"nope": "1"})


def test_environment_overrides_read_variables_and_files(tmp_path: Path) -> None:
    secret_file = tmp_path / "jf"
    secret_file.write_text("file-secret-value\n")
    overrides = environment_overrides(
        environ={
            "TINDEERR_SERVER_NAME": "Home",
            "TINDEERR_MEDIA_SERVER_API_KEY_FILE": str(secret_file),
            "TINDEERR_NOT_A_SETTING": "ignored",
        }
    )
    assert overrides == {"server_name": "Home", "media_server_api_key": "file-secret-value"}


@pytest.mark.parametrize(
    ("variable", "raw", "expected"),
    [
        ("EXCLUDE_ADULT", "false", False),
        ("EXCLUDE_ADULT", "False", False),
        ("EXCLUDE_ADULT", "0", False),
        ("EXCLUDE_ADULT", "true", True),
        ("EXCLUDE_ADULT", "yes", True),
        ("DAILY_CAP", "42", 42),
        ("GENRES", '["Horror", "Drama"]', ["Horror", "Drama"]),
        ("FILTERS", '{"exclude_adult": false}', {"exclude_adult": False}),
        ("SERVER_NAME", "123", "123"),
        ("SERVER_NAME", "null", "null"),
        ("SERVER_NAME", '"quoted"', '"quoted"'),
        ("MEDIA_SERVER_KIND", "plex", "plex"),
    ],
)
def test_environment_values_are_parsed_to_the_setting_type(
    variable: str, raw: str, expected: object
) -> None:
    name = variable.lower()
    overrides = environment_overrides(DEFINITIONS, {f"TINDEERR_{variable}": raw})
    assert overrides == {name: expected}
    assert type(overrides[name]) is type(expected)


@pytest.mark.parametrize(
    ("variable", "raw"),
    [
        ("EXCLUDE_ADULT", "maybe-hunter2"),
        ("DAILY_CAP", "lots-hunter2"),
        ("GENRES", "Horror-hunter2"),
        ("GENRES", '["hunter2", 3]'),
        ("MEDIA_SERVER_KIND", "kodi-hunter2"),
    ],
)
def test_invalid_environment_values_are_a_config_error(variable: str, raw: str) -> None:
    with pytest.raises(ConfigError) as caught:
        environment_overrides(DEFINITIONS, {f"TINDEERR_{variable}": raw})
    assert f"TINDEERR_{variable}" in str(caught.value)
    assert "hunter2" not in str(caught.value)


def test_every_invalid_variable_is_reported_at_once() -> None:
    with pytest.raises(ConfigError) as caught:
        environment_overrides(
            DEFINITIONS, {"TINDEERR_EXCLUDE_ADULT": "x", "TINDEERR_DAILY_CAP": "y"}
        )
    assert "TINDEERR_EXCLUDE_ADULT" in str(caught.value)
    assert "TINDEERR_DAILY_CAP" in str(caught.value)


async def test_typed_override_is_returned_typed(engine: AsyncEngine) -> None:
    overrides = environment_overrides(DEFINITIONS, {"TINDEERR_EXCLUDE_ADULT": "false"})
    store = make_store(engine, dict(overrides))
    value = await store.get("exclude_adult")
    assert (value.value, value.locked) == (False, True)


async def test_overrides_given_directly_are_validated(engine: AsyncEngine) -> None:
    with pytest.raises(InvalidSettingValueError):
        make_store(engine, {"daily_cap": "many"})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("exclude_adult", "not-a-bool"),
        ("daily_cap", "twenty"),
        ("server_name", 5),
        ("server_name", None),
        ("media_server_kind", "kodi"),
        ("genres", "Horror"),
        ("media_server_api_key", 12345678),
    ],
)
async def test_set_rejects_values_of_the_wrong_type(
    engine: AsyncEngine, name: str, value: object
) -> None:
    store = make_store(engine)
    with pytest.raises(InvalidSettingValueError) as caught:
        await store.set(name, value)
    assert (caught.value.status, caught.value.code) == (400, "validation_error")
    assert (await store.get(name)).source == "default"


async def test_set_normalises_lax_values(engine: AsyncEngine) -> None:
    store = make_store(engine)
    await store.set("daily_cap", "15")
    assert (await store.get("daily_cap")).value == 15
    assert await raw_value(engine, "daily_cap") == "15"


async def test_stored_value_of_the_wrong_type_falls_back_to_the_default(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    store = make_store(engine)
    await store.set("daily_cap", 15)
    async with engine.begin() as connection:
        await connection.execute(
            update(settings_table)
            .where(settings_table.c.name == "daily_cap")
            .values(value='"fifteen"')
        )
    value = await store.get("daily_cap")
    assert (value.value, value.source) == (20, "default")
    assert "does not fit its type" in caplog.text


@pytest.mark.parametrize("definition", list(SETTINGS.values()), ids=lambda d: d.name)
def test_defaults_fit_their_type(definition: SettingDefinition) -> None:
    assert definition.validate(definition.default) == definition.default


def test_setting_names_never_collide_with_bootstrap_configuration() -> None:
    # Both read TINDEERR_<NAME>: one variable must never mean two things.
    assert set(SETTINGS).isdisjoint(ServerConfig.model_fields)
