"""The optional connectors over HTTP: TMDb, OMDb, the request backend, the AI provider.

The whole application runs, with the **real** adapters wired to fake services, so what
these tests exercise is the path an administrator's browser really takes: the contract
body, the locks, the secret-reuse rule, the masking, and the coarse test result.

The rule under most of them is the security model's section 7: **a stored secret is
never sent to an address it was not stored for**. An administrator's session is the one
credential that can make this server call an address of somebody's choosing, and it must
not also be able to make it carry the keys it holds there.
"""

from typing import Any

import pytest
from fastapi import FastAPI

from tests.support import (
    API,
    USER_NAME,
    USER_PASSWORD,
    FakeInternet,
    app_login,
    build_app,
    console_client,
    console_headers,
    seed_jellyfin,
    set_up_server,
    web_login,
)
from tests.support.ai import ANTHROPIC_KEY, OLLAMA_URL, OPENAI_KEY
from tests.support.metadata import OMDB_API_KEY, TMDB_API_KEY, omdb_title
from tests.support.outside import COMPATIBLE_HOST, FakeOutside
from tests.support.requests_backend import SEERR_API_KEY, SEERR_URL, seerr_user
from tests.test_contract import assert_matches_contract

CONNECTORS = f"{API}/admin/connectors"
MODELS = f"{API}/admin/llm/models"
COMPATIBLE_URL = f"http://{COMPATIBLE_HOST}:4000/v1"


@pytest.fixture
def outside() -> FakeOutside:
    """Fake TMDb, OMDb, Seerr and AI providers, all reachable at once."""
    services = FakeOutside()
    services.omdb.titles["tt0111161"] = omdb_title()
    services.seerr.users = [seerr_user(1, "Alex", jellyfin_user_id="0" * 32)]
    return services


@pytest.fixture
def internet() -> FakeInternet:
    """The media servers, so the application can be set up and signed in to at all."""
    return seed_jellyfin(FakeInternet())


@pytest.fixture
def app(tmp_path: Any, internet: FakeInternet, outside: FakeOutside) -> FastAPI:
    """An application whose connectors reach the fake services and nothing else."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return build_app(data_dir, internet=internet, outside=outside)


def tmdb_body(**fields: Any) -> dict[str, Any]:
    """The body the console sends for the TMDb connector."""
    return {"connector": "tmdb", "api_key": TMDB_API_KEY} | fields


def omdb_body(**fields: Any) -> dict[str, Any]:
    """The body the console sends for the OMDb connector."""
    return {"connector": "omdb", "api_key": OMDB_API_KEY} | fields


def requests_body(**fields: Any) -> dict[str, Any]:
    """The body the console sends for the request backend."""
    return {"connector": "requests", "url": SEERR_URL, "api_key": SEERR_API_KEY} | fields


def llm_body(**fields: Any) -> dict[str, Any]:
    """The body the console sends for the AI provider."""
    return {
        "connector": "llm",
        "provider": "openai",
        "api_key": OPENAI_KEY,
        "model": "model-a",
    } | fields


def save(client: Any, csrf: str, kind: str, body: dict[str, Any]) -> Any:
    """Save one connector as the console does."""
    return client.put(f"{CONNECTORS}/{kind}", json=body, headers=console_headers(csrf))


def check_one(client: Any, csrf: str, kind: str, body: dict[str, Any]) -> Any:
    """Run one connector's connection test as the console does."""
    return client.post(f"{CONNECTORS}/{kind}/test", json=body, headers=console_headers(csrf))


# --- saving and listing -------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("tmdb", tmdb_body()),
        ("omdb", omdb_body()),
        ("requests", requests_body()),
        ("llm", llm_body()),
    ],
)
def test_every_optional_connector_can_be_saved(
    app: FastAPI, kind: str, body: dict[str, Any]
) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = save(client, csrf, kind, body)
        assert response.status_code == 200, response.text
        assert_matches_contract(f"{CONNECTORS}/{{kind}}", "put", response)
        assert response.json()["status"]["health"] == "ok"
        assert response.json()["configured"] is True
        listed = {row["kind"]: row for row in client.get(CONNECTORS).json()["connectors"]}
    assert listed[kind]["configured"] is True
    # Listing never calls anything, so it cannot know whether it still works.
    assert listed[kind]["status"]["health"] == "unknown"
    assert listed[kind]["status"]["checked_at"] is None


def test_every_kind_is_listed_even_before_it_exists(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        response = client.get(CONNECTORS)
        assert_matches_contract(CONNECTORS, "get", response)
        listed = response.json()["connectors"]
    assert [row["kind"] for row in listed] == [
        "media_server",
        "requests",
        "tmdb",
        "omdb",
        "llm",
    ]
    assert [row["status"]["health"] for row in listed[1:]] == ["not_configured"] * 4


def test_a_secret_is_never_returned(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        body = save(client, csrf, "tmdb", tmdb_body()).json()
    assert body["secret"]["set"] is True
    assert body["secret"]["last4"] == TMDB_API_KEY[-4:]
    assert TMDB_API_KEY not in str(body)


def test_a_short_secret_shows_nothing_at_all(app: FastAPI, outside: FakeOutside) -> None:
    outside.omdb.api_key = "short"
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        body = save(client, csrf, "omdb", omdb_body(api_key="short")).json()
    # Four characters of a five-character key are the key.
    assert body["secret"]["set"] is True
    assert body["secret"]["last4"] is None


def test_the_request_backend_keeps_its_own_fields(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        body = save(
            client, csrf, "requests", requests_body(tv_seasons="first", verify_tls=False)
        ).json()
    assert body["url"] == SEERR_URL
    assert body["tv_seasons"] == "first"
    assert body["verify_tls"] is False


def test_the_ai_connector_shows_its_provider_and_model(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        body = save(client, csrf, "llm", llm_body()).json()
    assert body["provider"] == "openai"
    assert body["model"] == "model-a"


def test_a_connector_that_fails_its_test_is_not_saved(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = save(client, csrf, "tmdb", tmdb_body(api_key="wrong-key-entirely"))
        assert response.status_code == 502
        assert response.json()["code"] == "connector_unauthorized"
        listed = {row["kind"]: row for row in client.get(CONNECTORS).json()["connectors"]}
    assert listed["tmdb"]["configured"] is False


def test_a_test_saves_nothing(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = check_one(client, csrf, "tmdb", tmdb_body())
        assert response.status_code == 200, response.text
        assert_matches_contract(f"{CONNECTORS}/{{kind}}/test", "post", response)
        assert response.json()["health"] == "ok"
        assert response.json()["checked_at"] is not None
        listed = {row["kind"]: row for row in client.get(CONNECTORS).json()["connectors"]}
    assert listed["tmdb"]["configured"] is False


def test_a_services_own_words_are_cut_to_a_plausible_length(
    app: FastAPI, outside: FakeOutside
) -> None:
    outside.seerr.version = "2.7.3" + "!" * 5000

    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = check_one(client, csrf, "requests", requests_body())

    version = response.json()["server_version"]
    assert version is not None
    assert len(version) == 64


def test_a_test_never_reflects_what_the_service_said(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = check_one(client, csrf, "requests", requests_body(url="http://nowhere.lan"))
    assert response.status_code == 200
    body = response.json()
    assert body["health"] == "unreachable"
    assert set(body) == {"health", "server_name", "server_version", "checked_at"}


def test_a_connector_can_be_removed(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert save(client, csrf, "tmdb", tmdb_body()).status_code == 200
        assert client.delete(f"{CONNECTORS}/tmdb", headers=console_headers(csrf)).status_code == (
            204
        )
        listed = {row["kind"]: row for row in client.get(CONNECTORS).json()["connectors"]}
    assert listed["tmdb"]["configured"] is False


# --- the secret-reuse rule ----------------------------------------------------------


def test_a_key_omitted_for_the_same_address_is_reused(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert save(client, csrf, "requests", requests_body()).status_code == 200
        response = client.put(
            f"{CONNECTORS}/requests",
            json={"connector": "requests", "url": SEERR_URL, "tv_seasons": "first"},
            headers=console_headers(csrf),
        )
    assert response.status_code == 200, response.text
    assert response.json()["tv_seasons"] == "first"


def test_a_key_omitted_for_a_new_address_is_refused(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert save(client, csrf, "requests", requests_body()).status_code == 200
        response = client.put(
            f"{CONNECTORS}/requests",
            json={"connector": "requests", "url": "http://elsewhere.lan:5055"},
            headers=console_headers(csrf),
        )
    assert response.status_code == 409
    assert response.json()["code"] == "secret_required"


def test_turning_tls_verification_off_needs_the_key_again(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert save(client, csrf, "requests", requests_body()).status_code == 200
        response = client.put(
            f"{CONNECTORS}/requests",
            json={"connector": "requests", "url": SEERR_URL, "verify_tls": False},
            headers=console_headers(csrf),
        )
    assert response.status_code == 409
    assert response.json()["code"] == "secret_required"


def test_changing_the_ai_provider_needs_the_key_again(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert save(client, csrf, "llm", llm_body()).status_code == 200
        response = client.put(
            f"{CONNECTORS}/llm",
            json={"connector": "llm", "provider": "anthropic", "model": "model-a"},
            headers=console_headers(csrf),
        )
    assert response.status_code == 409
    assert response.json()["code"] == "secret_required"


def test_changing_the_ai_address_needs_the_key_again(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert (
            save(
                client,
                csrf,
                "llm",
                llm_body(provider="openai_compatible", base_url=COMPATIBLE_URL),
            ).status_code
            == 200
        )
        response = client.put(
            f"{CONNECTORS}/llm",
            json={
                "connector": "llm",
                "provider": "openai_compatible",
                "base_url": "http://elsewhere.lan/v1",
                "model": "model-a",
            },
            headers=console_headers(csrf),
        )
    assert response.status_code == 409
    assert response.json()["code"] == "secret_required"


def test_changing_only_the_model_keeps_the_key(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert save(client, csrf, "llm", llm_body()).status_code == 200
        response = client.put(
            f"{CONNECTORS}/llm",
            json={"connector": "llm", "provider": "openai", "model": "model-b"},
            headers=console_headers(csrf),
        )
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "model-b"


def test_tmdb_has_one_host_so_its_key_is_always_reusable(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert save(client, csrf, "tmdb", tmdb_body()).status_code == 200
        response = client.put(
            f"{CONNECTORS}/tmdb", json={"connector": "tmdb"}, headers=console_headers(csrf)
        )
    assert response.status_code == 200, response.text


def test_a_first_save_without_a_key_is_refused(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.put(
            f"{CONNECTORS}/tmdb", json={"connector": "tmdb"}, headers=console_headers(csrf)
        )
    assert response.status_code == 409
    assert response.json()["code"] == "secret_required"


def test_ollama_needs_no_key(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = save(
            client,
            csrf,
            "llm",
            {
                "connector": "llm",
                "provider": "ollama",
                "base_url": OLLAMA_URL,
                "model": "model-a",
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["secret"]["set"] is False


# --- the model picker ---------------------------------------------------------------


def test_the_models_of_a_provider_can_be_listed(app: FastAPI, outside: FakeOutside) -> None:
    outside.openai.models = ["model-b", "model-a"]
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.post(MODELS, json=llm_body(), headers=console_headers(csrf))
    assert response.status_code == 200, response.text
    assert_matches_contract(MODELS, "post", response)
    assert response.json() == {"models": ["model-a", "model-b"]}


def test_listing_models_uses_the_stored_key_for_the_same_provider(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert save(client, csrf, "llm", llm_body()).status_code == 200
        response = client.post(
            MODELS,
            json={"connector": "llm", "provider": "openai"},
            headers=console_headers(csrf),
        )
    assert response.status_code == 200, response.text


def test_listing_models_never_sends_the_stored_key_elsewhere(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert save(client, csrf, "llm", llm_body()).status_code == 200
        response = client.post(
            MODELS,
            json={
                "connector": "llm",
                "provider": "openai_compatible",
                "base_url": COMPATIBLE_URL,
            },
            headers=console_headers(csrf),
        )
    assert response.status_code == 409
    assert response.json()["code"] == "secret_required"


def test_a_provider_that_refuses_the_key_is_reported(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.post(
            MODELS, json=llm_body(api_key="sk-wrong"), headers=console_headers(csrf)
        )
    assert response.status_code == 502
    assert_matches_contract(MODELS, "post", response)
    assert response.json()["code"] == "llm_auth_failed"


def test_anthropic_models_are_listed_too(app: FastAPI, outside: FakeOutside) -> None:
    outside.anthropic.models = ["claude-a"]
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.post(
            MODELS,
            json={"connector": "llm", "provider": "anthropic", "api_key": ANTHROPIC_KEY},
            headers=console_headers(csrf),
        )
    assert response.status_code == 200, response.text
    assert response.json()["models"] == ["claude-a"]


# --- who may do this ----------------------------------------------------------------


def test_only_an_administrator_may_touch_a_connector(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        client.cookies.clear()
        csrf = web_login(client, USER_NAME, USER_PASSWORD).json()["csrf_token"]
        response = save(client, csrf, "tmdb", tmdb_body())
    assert response.status_code == 403
    assert response.json()["code"] == "admin_required"


def test_a_phone_may_not_touch_a_connector(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        tokens = app_login(client).json()
    with console_client(app) as phone:
        response = phone.put(
            f"{CONNECTORS}/tmdb",
            json=tmdb_body(),
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
    assert response.status_code == 401


def test_a_body_that_is_not_this_kind_is_refused(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = save(client, csrf, "tmdb", omdb_body())
    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"


def test_an_unknown_connector_kind_does_not_exist(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.put(
            f"{CONNECTORS}/trakt", json=tmdb_body(), headers=console_headers(csrf)
        )
    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"


def test_the_tests_are_rate_limited(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        statuses = [check_one(client, csrf, "tmdb", tmdb_body()).status_code for _ in range(12)]
    assert 429 in statuses


def test_the_console_host_is_the_only_origin_accepted(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.put(
            f"{CONNECTORS}/tmdb",
            json=tmdb_body(),
            headers={"Origin": "https://evil.test", "X-CSRF-Token": csrf},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"


# --- what the environment locks -----------------------------------------------------


def test_a_key_set_by_the_environment_cannot_be_changed_here(
    tmp_path: Any,
    internet: FakeInternet,
    outside: FakeOutside,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TINDARR_TMDB_API_KEY", TMDB_API_KEY)
    data_dir = tmp_path / "locked"
    data_dir.mkdir()
    locked = build_app(data_dir, internet=internet, outside=outside)

    with console_client(locked) as client:
        csrf = set_up_server(client, locked)
        refused = save(client, csrf, "tmdb", tmdb_body(api_key="another-key"))
        listed = {row["kind"]: row for row in client.get(CONNECTORS).json()["connectors"]}
        # The operator's own value is what the connector uses, and it tests fine.
        accepted = check_one(client, csrf, "tmdb", {"connector": "tmdb"})

    assert refused.status_code == 409
    assert refused.json()["code"] == "setting_locked"
    assert listed["tmdb"]["configured"] is True
    assert listed["tmdb"]["locked_fields"] == ["api_key"]
    assert listed["tmdb"]["secret"]["locked"] is True
    assert accepted.json()["health"] == "ok"


def test_a_locked_connector_cannot_be_removed(
    tmp_path: Any,
    internet: FakeInternet,
    outside: FakeOutside,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TINDARR_OMDB_API_KEY", OMDB_API_KEY)
    data_dir = tmp_path / "locked-omdb"
    data_dir.mkdir()
    locked = build_app(data_dir, internet=internet, outside=outside)

    with console_client(locked) as client:
        csrf = set_up_server(client, locked)
        response = client.delete(f"{CONNECTORS}/omdb", headers=console_headers(csrf))

    assert response.status_code == 409
    assert response.json()["code"] == "setting_locked"


def test_a_pinned_key_cannot_be_sent_to_a_new_address(
    tmp_path: Any,
    internet: FakeInternet,
    outside: FakeOutside,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the rule: a stolen console session is not a courier."""
    monkeypatch.setenv("TINDARR_REQUESTS_API_KEY", SEERR_API_KEY)
    data_dir = tmp_path / "pinned"
    data_dir.mkdir()
    pinned = build_app(data_dir, internet=internet, outside=outside)

    with console_client(pinned) as client:
        csrf = set_up_server(client, pinned)
        assert (
            save(client, csrf, "requests", {"connector": "requests", "url": SEERR_URL}).status_code
            == 200
        )
        moved = check_one(
            client, csrf, "requests", {"connector": "requests", "url": "http://collector.lan"}
        )
        downgraded = check_one(
            client,
            csrf,
            "requests",
            {"connector": "requests", "url": SEERR_URL, "verify_tls": False},
        )
        removed = client.delete(f"{CONNECTORS}/requests", headers=console_headers(csrf))

    assert moved.status_code == 409
    assert moved.json()["code"] == "setting_locked"
    assert downgraded.status_code == 409
    # And removal is not a way around it: an unconfigured connector with a pinned key
    # would accept any first address.
    assert removed.status_code == 409
    assert removed.json()["code"] == "setting_locked"


def test_a_pinned_ai_key_cannot_follow_a_change_of_provider(
    tmp_path: Any,
    internet: FakeInternet,
    outside: FakeOutside,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TINDARR_LLM_API_KEY", OPENAI_KEY)
    data_dir = tmp_path / "pinned-llm"
    data_dir.mkdir()
    pinned = build_app(data_dir, internet=internet, outside=outside)

    with console_client(pinned) as client:
        csrf = set_up_server(client, pinned)
        assert (
            save(
                client, csrf, "llm", {"connector": "llm", "provider": "openai", "model": "model-a"}
            ).status_code
            == 200
        )
        elsewhere = client.post(
            MODELS,
            json={
                "connector": "llm",
                "provider": "openai_compatible",
                "base_url": COMPATIBLE_URL,
            },
            headers=console_headers(csrf),
        )

    assert elsewhere.status_code == 409
    assert elsewhere.json()["code"] == "setting_locked"


def test_a_pinned_address_is_shown_so_the_key_can_still_be_entered(
    tmp_path: Any,
    internet: FakeInternet,
    outside: FakeOutside,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator pins the address in compose; the administrator types the key."""
    monkeypatch.setenv("TINDARR_REQUESTS_URL", SEERR_URL)
    data_dir = tmp_path / "pinned-url"
    data_dir.mkdir()
    pinned = build_app(data_dir, internet=internet, outside=outside)

    with console_client(pinned) as client:
        csrf = set_up_server(client, pinned)
        listed = {row["kind"]: row for row in client.get(CONNECTORS).json()["connectors"]}
        without = save(client, csrf, "requests", {"connector": "requests", "url": SEERR_URL})
        with_key = save(client, csrf, "requests", requests_body())

    # The console can show and pre-fill the pinned address even though nothing is
    # stored, which is what makes the card usable at all.
    assert listed["requests"]["configured"] is False
    assert listed["requests"]["url"] == SEERR_URL
    assert listed["requests"]["locked_fields"] == ["url"]
    # The key is still the administrator's to enter, and only then does it work.
    assert without.status_code == 409
    assert without.json()["code"] == "secret_required"
    assert with_key.status_code == 200, with_key.text
