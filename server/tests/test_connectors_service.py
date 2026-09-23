"""The connector service on its own: what it stores, and what it builds from it.

The HTTP tests cover the administrator's path. These cover the other side — the one the
swipe engine will use from step 4 — and the corners a well-formed request body cannot
reach.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.ai import OLLAMA_URL, OPENAI_KEY
from tests.support.metadata import OMDB_API_KEY, TMDB_API_KEY, omdb_title
from tests.support.outside import FakeOutside
from tests.support.requests_backend import SEERR_API_KEY, SEERR_URL
from tindarr.adapters.omdb import OmdbRatings
from tindarr.adapters.seerr import SeerrBackend
from tindarr.adapters.tmdb import TmdbMetadata
from tindarr.connectors import ConnectorInput, ConnectorService
from tindarr.core.crypto import SecretCipher
from tindarr.core.errors import ProblemError
from tindarr.core.keys import KeyMaterial
from tindarr.storage.settings import SettingsStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def outside() -> FakeOutside:
    """Fake TMDb, OMDb, Seerr and AI providers."""
    services = FakeOutside()
    services.omdb.titles["tt0111161"] = omdb_title()
    return services


@pytest.fixture
def service(settings_store: SettingsStore, outside: FakeOutside) -> ConnectorService:
    """The service on the test database, wired to the fakes."""
    return ConnectorService(settings_store, outside.factories)


def given(**fields: object) -> ConnectorInput:
    """Build a request that carries exactly the fields it names."""
    kind = fields.pop("kind")
    assert isinstance(kind, str)
    return ConnectorInput(kind=kind, given=frozenset(fields), **fields)  # pyright: ignore[reportArgumentType]


# --- nothing configured -------------------------------------------------------------


async def test_nothing_is_configured_to_begin_with(service: ConnectorService) -> None:
    assert await service.tmdb() is None
    assert await service.omdb() is None
    assert await service.requests() is None
    assert await service.llm() is None
    assert await service.metadata() is None
    assert await service.ratings() is None
    assert await service.request_backend() is None
    assert await service.llm_provider() is None
    for kind in ("tmdb", "omdb", "requests", "llm"):
        assert await service.state(kind) is None  # pyright: ignore[reportArgumentType]


# --- what the engine gets -----------------------------------------------------------


async def test_the_adapters_are_built_from_what_was_stored(
    service: ConnectorService,
) -> None:
    await service.save(given(kind="tmdb", api_key=TMDB_API_KEY))
    await service.save(given(kind="omdb", api_key=OMDB_API_KEY))
    await service.save(given(kind="requests", url=SEERR_URL, api_key=SEERR_API_KEY))
    await service.save(given(kind="llm", provider="ollama", base_url=OLLAMA_URL, model="model-a"))

    assert isinstance(await service.metadata(), TmdbMetadata)
    assert isinstance(await service.ratings(), OmdbRatings)
    assert isinstance(await service.request_backend(), SeerrBackend)
    provider = await service.llm_provider()
    assert provider is not None
    assert provider.kind == "ollama"


async def test_the_request_backend_follows_the_media_server(
    service: ConnectorService, settings_store: SettingsStore
) -> None:
    assert await service.media_server_kind() == "jellyfin"
    await settings_store.set("media_server_kind", "plex")
    assert await service.media_server_kind() == "plex"


# --- corners a well-formed body cannot reach ----------------------------------------


async def test_a_request_backend_with_no_address_is_refused(
    service: ConnectorService,
) -> None:
    with pytest.raises(ProblemError) as failure:
        await service.check(ConnectorInput(kind="requests", api_key=SEERR_API_KEY))
    assert failure.value.code == "validation_error"


async def test_an_ai_connector_with_no_provider_is_refused(
    service: ConnectorService,
) -> None:
    with pytest.raises(ProblemError) as failure:
        await service.check(ConnectorInput(kind="llm", api_key=OPENAI_KEY))
    assert failure.value.code == "validation_error"


async def test_a_stored_value_that_is_not_a_provider_reads_as_none(
    service: ConnectorService, settings_store: SettingsStore
) -> None:
    # The setting's own type refuses this, so it can only come from a hand-edited row.
    await settings_store.set("llm_provider", None)
    assert await service.llm() is None


async def test_an_unknown_reasoning_effort_is_dropped(
    service: ConnectorService, settings_store: SettingsStore
) -> None:
    await service.save(given(kind="llm", provider="openai", api_key=OPENAI_KEY, model="model-a"))
    stored = await service.llm()
    assert stored is not None
    assert stored.reasoning_effort is None


async def test_a_reasoning_effort_is_kept(service: ConnectorService) -> None:
    await service.save(
        given(
            kind="llm",
            provider="openai",
            api_key=OPENAI_KEY,
            model="model-a",
            reasoning_effort="high",
        )
    )
    stored = await service.llm()
    assert stored is not None
    assert stored.reasoning_effort == "high"


async def test_an_incomplete_request_backend_reads_as_nothing(
    service: ConnectorService, settings_store: SettingsStore
) -> None:
    await settings_store.set("requests_url", SEERR_URL)
    assert await service.requests() is None


async def test_removing_what_was_never_there_changes_nothing(
    service: ConnectorService,
) -> None:
    await service.remove("tmdb")
    assert await service.tmdb() is None


async def test_a_failing_test_stores_nothing(
    service: ConnectorService, outside: FakeOutside
) -> None:
    outside.tmdb.offline = True
    with pytest.raises(ProblemError) as failure:
        await service.save(given(kind="tmdb", api_key=TMDB_API_KEY))
    assert failure.value.code == "connector_unreachable"
    assert await service.tmdb() is None


async def test_the_state_of_each_kind(service: ConnectorService) -> None:
    await service.save(given(kind="omdb", api_key=OMDB_API_KEY))
    state = await service.state("omdb")
    assert state is not None
    assert state.secret == OMDB_API_KEY
    assert state.url is None


@pytest.fixture
def settings_store(engine: AsyncEngine, keys: KeyMaterial) -> SettingsStore:
    """A settings store with nothing forced by the environment."""
    return SettingsStore(engine, SecretCipher.for_settings(keys), {})
