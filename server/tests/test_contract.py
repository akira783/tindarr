"""The server honours the committed contract, api/openapi.yaml.

The generated spec is not compared byte for byte (too brittle). Instead:

- the responses of implemented endpoints are validated against the committed schemas;
- every route the app exposes under /api/v1 (and /healthz) must be in the contract, with
  the same method and operationId, so no undocumented endpoint can ship.

Contract operations not implemented yet are fine.
"""

# jsonschema and referencing are only partially typed.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

from collections.abc import Iterator, Mapping
from functools import cache
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from tests.support import server_config
from tindarr.core.config import ServerConfig
from tindarr.main.app import create_app

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"
CONTRACT_URI = "urn:tindarr:openapi"
HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}


@cache
def contract() -> Mapping[str, Any]:
    return cast("Mapping[str, Any]", yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8")))


@cache
def registry() -> Registry[Any]:
    resource: Resource[Any] = Resource.from_contents(contract(), default_specification=DRAFT202012)
    return Registry().with_resource(CONTRACT_URI, resource)


def pointer(*parts: str) -> str:
    return "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def assert_matches_contract(path: str, method: str, response: httpx2.Response) -> None:
    """Validate status, media type and body against the contract operation."""
    operation = contract()["paths"][path][method]
    status = str(response.status_code)
    assert status in operation["responses"], f"{method} {path}: {status} is not documented"
    documented = operation["responses"][status]
    ref = documented.get("$ref")
    location = (
        ref.removeprefix("#/") if ref else pointer("paths", path, method, "responses", status)
    )
    documented = resolve(location)
    media_type = response.headers["content-type"].split(";")[0]
    assert media_type in documented["content"], f"{method} {path}: undocumented {media_type}"
    schema_ref = f"{CONTRACT_URI}#/{location}/content/{pointer(media_type)}/schema"
    validator = Draft202012Validator({"$ref": schema_ref}, registry=registry())
    errors = [error.message for error in validator.iter_errors(response.json())]
    assert errors == [], f"{method} {path}: {errors}"


def resolve(location: str) -> Mapping[str, Any]:
    node: Any = contract()
    for part in location.split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return cast("Mapping[str, Any]", node)


def operation_of(response: httpx2.Response) -> Mapping[str, Any] | None:
    """The contract operation a response came from, or ``None`` if there is none.

    The request's own URL is matched against the contract's paths, templated segments
    included, so an error-path assertion checks the documented operation without the
    test having to name it. A request to something the contract does not describe at
    all (an unknown path, a method that operation does not have) has no operation, and
    only the ``Problem`` schema applies to it.
    """
    wanted = response.request.url.path.strip("/").split("/")
    for candidate, item in contract()["paths"].items():
        parts = candidate.strip("/").split("/")
        if len(parts) != len(wanted):
            continue
        pairs = zip(parts, wanted, strict=True)
        if all(part.startswith("{") or part == seen for part, seen in pairs):
            method = response.request.method.lower()
            return cast("Mapping[str, Any] | None", item.get(method))
    return None


def assert_is_problem(response: httpx2.Response, status: int, code: str) -> None:
    """Assert the response is the documented problem, with the documented status."""
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    operation = operation_of(response)
    if operation is not None:
        documented = operation["responses"]
        assert str(status) in documented, (
            f"{response.request.method} {response.request.url.path}: {status} is not documented"
        )
        description = str(resolve_description(documented[str(status)]))
        assert code in description, (
            f"{response.request.method} {response.request.url.path}: "
            f"{status} does not document `{code}`"
        )
    validator = Draft202012Validator(
        {"$ref": f"{CONTRACT_URI}#/components/schemas/Problem"}, registry=registry()
    )
    body = response.json()
    assert [error.message for error in validator.iter_errors(body)] == []
    assert body["code"] == code


def resolve_description(documented: Mapping[str, Any]) -> object:
    """The description of a documented response, following a ``$ref`` if there is one."""
    ref = documented.get("$ref")
    node = resolve(ref.removeprefix("#/")) if ref else documented
    return node.get("description", "")


def exposed_operations(app: FastAPI) -> Iterator[tuple[str, str, str | None]]:
    """(path, method, operationId) of every route the app serves under /api/v1 or /healthz.

    Read from the spec FastAPI generates, the public view of its routes. A route hidden
    with ``include_in_schema=False`` would escape it, hence the test forbidding that flag.
    """
    for path, item in app.openapi()["paths"].items():
        if path != "/healthz" and not path.startswith("/api/v1/"):
            continue
        for method, operation in item.items():
            yield path, method, operation.get("operationId")


def test_healthz_matches_contract(client: TestClient) -> None:
    assert_matches_contract("/healthz", "get", client.get("/healthz"))


def test_server_info_matches_contract(client: TestClient) -> None:
    assert_matches_contract("/api/v1/server/info", "get", client.get("/api/v1/server/info"))


def test_configured_server_info_matches_contract(
    config: ServerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TINDARR_MEDIA_SERVER_KIND", "plex")
    with TestClient(create_app(config)) as client:
        response = client.get("/api/v1/server/info")
    assert_matches_contract("/api/v1/server/info", "get", response)


def test_errors_are_contract_problems(client: TestClient) -> None:
    assert_is_problem(client.get("/api/v1/does-not-exist"), 404, "not_found")
    assert_is_problem(client.delete("/api/v1/server/info"), 405, "method_not_allowed")


def test_error_codes_are_documented() -> None:
    description = contract()["components"]["schemas"]["Problem"]["properties"]["code"][
        "description"
    ]
    for code in (
        "validation_error",
        "unauthorized",
        "forbidden",
        "not_found",
        "method_not_allowed",
        "rate_limited",
        "setting_locked",
        "bad_request",
        "internal_error",
    ):
        assert code in description


@pytest.mark.parametrize("api_docs", [False, True])
def test_every_exposed_route_is_in_the_contract(data_dir: Path, api_docs: bool) -> None:
    app = create_app(server_config(data_dir, api_docs=api_docs))
    routes = list(exposed_operations(app))
    assert ("/healthz", "get", "health") in routes
    assert ("/api/v1/server/info", "get", "getServerInfo") in routes
    paths = contract()["paths"]
    for path, method, operation_id in routes:
        assert path in paths, f"{path} is not in api/openapi.yaml"
        assert method in paths[path], f"{method.upper()} {path} is not in api/openapi.yaml"
        assert operation_id == paths[path][method]["operationId"], f"{method} {path}"


def test_no_route_is_hidden_from_the_generated_spec() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "tindarr"
    offenders = [
        str(path) for path in source.rglob("*.py") if "include_in_schema" in path.read_text()
    ]
    assert offenders == []


def test_contract_paths_use_known_methods() -> None:
    for path, item in contract()["paths"].items():
        assert set(item) <= HTTP_METHODS | {"parameters", "summary", "description"}, path


def test_a_problem_the_contract_does_not_document_fails_the_assertion(
    client: TestClient,
) -> None:
    # The guard of assert_is_problem itself: an undocumented code must not pass.
    response = client.post("/api/v1/setup/claim", json={"setup_code": "x" * 12})
    assert_is_problem(response, 403, "https_required")
    with pytest.raises(AssertionError, match="does not document"):
        assert_is_problem(response, 403, "not_a_server_user")
