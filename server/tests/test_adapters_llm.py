"""The AI providers, driven through their real SDKs against recorded answers.

Three things are being held in place here. **Nothing a model writes escapes the
schema**: the adapters return an instance of a Pydantic model and never text. **A
failure has one of five names**, whichever provider produced it, so the console and the
app can say something useful about a key, a model, a plan or an address. And **the retry
never quotes the model back to itself**, which is the one thing that would turn a
malformed answer into a way in.

The roadmap also asks for one live run per provider with a real key. That needs the
owner's own keys and costs money, so it is not run here and no test pretends to.
"""

import json
from typing import Any

import pytest
from pydantic import BaseModel, Field

from tests.support.ai import (
    ANTHROPIC_KEY,
    COMPATIBLE_URL,
    GEMINI_KEY,
    OLLAMA_URL,
    OPENAI_KEY,
    FakeAnthropic,
    FakeGemini,
    FakeOllama,
    FakeOpenAi,
    body_of,
)
from tindarr.adapters.llm import (
    InvalidOutputError,
    extract_json,
    json_schema_of,
    llm_provider_factory,
    parse_structured,
    repair_title_qualifiers,
    strip_fences,
)
from tindarr.adapters.llm.anthropic_provider import AnthropicProvider
from tindarr.adapters.llm.gemini_provider import GeminiProvider
from tindarr.adapters.llm.ollama_provider import OllamaProvider
from tindarr.adapters.llm.openai_provider import MISTRAL_BASE_URL, OpenAiProvider
from tindarr.core.errors import ProblemError
from tindarr.ports.llm import LlmConnection, LlmProvider, Prompt

pytestmark = pytest.mark.anyio


class Pick(BaseModel):
    """One suggestion, as the swipe engine will ask for them."""

    title: str
    year: int | None = None


class Batch(BaseModel):
    """What a provider is asked to produce."""

    cards: list[Pick] = Field(default_factory=list[Pick])


PROMPT = Prompt(
    instructions="You pick films.", message="Pick two.", temperature=0.8, max_output_tokens=900
)
ONE_CARD = '{"cards": [{"title": "Arrival", "year": 2016}]}'


# --- repair -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('Here you go:\n{"a": 1}\nHope that helps.', '{"a": 1}'),
        ('{"a": {"b": [1, 2]}}', '{"a": {"b": [1, 2]}}'),
        ("[1, 2]", "[1, 2]"),
        ("no json at all", "no json at all"),
        ('{"a": 1', '{"a": 1'),
    ],
)
def test_an_answer_is_dug_out_of_whatever_surrounds_it(raw: str, expected: str) -> None:
    assert extract_json(strip_fences(raw)) == expected


def test_a_split_title_and_its_year_are_joined() -> None:
    assert repair_title_qualifiers('{"t": "Blade Runner" (1982)}') == (
        '{"t": "Blade Runner (1982)"}'
    )


def test_a_bare_array_is_put_back_in_its_envelope() -> None:
    found = parse_structured('[{"title": "Arrival"}]', Batch)
    assert [pick.title for pick in found.cards] == ["Arrival"]


def test_an_envelope_with_the_wrong_name_is_renamed() -> None:
    found = parse_structured('{"results": [{"title": "Arrival"}]}', Batch)
    assert [pick.title for pick in found.cards] == ["Arrival"]


def test_an_answer_with_two_lists_is_not_guessed_at() -> None:
    # Neither list is renamed into ``cards``: guessing here would serve somebody a
    # recommendation the model never made.
    assert parse_structured('{"a": [], "b": [{"title": "x"}]}', Batch).cards == []


def test_a_schema_with_no_single_list_is_left_alone() -> None:
    assert parse_structured('{"title": "Arrival"}', Pick).title == "Arrival"


def test_anything_outside_the_schema_is_dropped() -> None:
    found = parse_structured(
        '{"cards": [{"title": "Arrival", "year": 2016, "run": "rm -rf /"}]}', Batch
    )
    assert found.cards[0].title == "Arrival"
    assert not hasattr(found.cards[0], "run")


def test_output_that_is_not_json_is_refused() -> None:
    with pytest.raises(InvalidOutputError, match="not_json"):
        parse_structured("I would rather not.", Batch)


def test_output_of_the_wrong_shape_is_refused() -> None:
    with pytest.raises(InvalidOutputError, match="schema_mismatch"):
        parse_structured('{"cards": [{"year": "soon"}]}', Batch)


def test_a_schema_is_tightened_for_strict_modes() -> None:
    schema = json_schema_of(Batch)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["cards"]
    card = schema["$defs"]["Pick"]
    assert card["additionalProperties"] is False
    assert sorted(card["required"]) == ["title", "year"]


# --- OpenAI, Mistral and the compatible endpoints -----------------------------------


def openai_provider(fake: FakeOpenAi, **fields: Any) -> OpenAiProvider:
    """A real OpenAI adapter talking to ``fake``."""
    fields.setdefault("api_key", OPENAI_KEY)
    api_key = str(fields.pop("api_key"))
    return OpenAiProvider(api_key, "model-a", transport=fake.transport, **fields)


@pytest.fixture
def openai_fake() -> FakeOpenAi:
    """An OpenAI-compatible endpoint that answers one card."""
    return FakeOpenAi(answers=[ONE_CARD])


async def test_openai_generates_a_validated_object(openai_fake: FakeOpenAi) -> None:
    found = await openai_provider(openai_fake).generate(PROMPT, Batch)

    assert [pick.title for pick in found.value.cards] == ["Arrival"]
    assert found.model == "model-a"
    assert found.usage.input_tokens == 11
    assert found.usage.output_tokens == 7
    assert not found.retried


async def test_the_request_carries_the_schema_and_the_dials(openai_fake: FakeOpenAi) -> None:
    await openai_provider(openai_fake).generate(PROMPT, Batch)

    body = body_of(openai_fake.requests[-1])
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["required"] == ["cards"]
    assert body["temperature"] == 0.8
    # The reasoning models refuse the older name outright.
    assert body["max_completion_tokens"] == 900
    assert "max_tokens" not in body
    assert body["messages"][0]["content"] == "You pick films."


async def test_a_bad_answer_is_retried_once_without_quoting_it_back(
    openai_fake: FakeOpenAi,
) -> None:
    openai_fake.answers = ["I would rather not.", ONE_CARD]

    found = await openai_provider(openai_fake).generate(PROMPT, Batch)

    assert found.retried
    assert [pick.title for pick in found.value.cards] == ["Arrival"]
    completions = [r for r in openai_fake.requests if r.url.path.endswith("/chat/completions")]
    assert len(completions) == 2
    retry = body_of(completions[1])
    assert retry["messages"][1]["content"] == "Pick two."
    # The model's own broken answer is nowhere in the second prompt.
    assert "rather not" not in json.dumps(retry)
    assert "did not match the required JSON schema" in retry["messages"][0]["content"]


async def test_two_bad_answers_are_a_reported_failure(openai_fake: FakeOpenAi) -> None:
    openai_fake.answers = ["nope", "still nope"]

    with pytest.raises(ProblemError) as failure:
        await openai_provider(openai_fake).generate(PROMPT, Batch)
    assert failure.value.code == "llm_invalid_output"


async def test_an_endpoint_that_refuses_a_schema_gets_a_simpler_request(
    openai_fake: FakeOpenAi,
) -> None:
    openai_fake.fails = [
        (400, {"error": {"message": "response_format.json_schema is not supported"}}),
        (400, {"error": {"message": "Unsupported parameter: response_format"}}),
    ]
    openai_fake.answers = [ONE_CARD]

    found = await openai_provider(openai_fake).generate(PROMPT, Batch)

    assert [pick.title for pick in found.value.cards] == ["Arrival"]
    bodies = [body_of(r) for r in openai_fake.requests if r.url.path.endswith("/chat/completions")]
    assert bodies[0]["response_format"]["type"] == "json_schema"
    assert bodies[1]["response_format"]["type"] == "json_object"
    assert "response_format" not in bodies[2]


async def test_a_reasoning_effort_is_dropped_when_the_endpoint_says_no(
    openai_fake: FakeOpenAi,
) -> None:
    openai_fake.fails = [(400, {"error": {"message": "Unsupported value: reasoning_effort"}})]
    openai_fake.answers = [ONE_CARD]

    await openai_provider(openai_fake, reasoning_effort="high").generate(PROMPT, Batch)

    bodies = [body_of(r) for r in openai_fake.requests if r.url.path.endswith("/chat/completions")]
    assert bodies[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in bodies[1]


async def test_a_bad_request_that_is_not_about_a_parameter_is_reported(
    openai_fake: FakeOpenAi,
) -> None:
    openai_fake.fails = [(400, {"error": {"message": "context length exceeded"}})]

    with pytest.raises(ProblemError) as failure:
        await openai_provider(openai_fake).generate(PROMPT, Batch)
    assert failure.value.code == "llm_unreachable"


async def test_an_answer_with_no_content_is_invalid_output(openai_fake: FakeOpenAi) -> None:
    openai_fake.fails = []
    openai_fake.answers = [ONE_CARD]
    provider = openai_provider(openai_fake)
    openai_fake.answers = []
    openai_fake.answers = [ONE_CARD]
    # A provider that answers with no choices at all.
    openai_fake.fails = [
        (200, {"id": "x", "object": "chat.completion", "created": 0, "model": "m", "choices": []})
    ]

    with pytest.raises(ProblemError) as failure:
        await provider.generate(PROMPT, Batch)
    assert failure.value.code == "llm_invalid_output"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "llm_auth_failed"),
        (403, "llm_auth_failed"),
        (404, "llm_model_not_found"),
        (429, "llm_quota"),
        (500, "llm_unreachable"),
        (503, "llm_unreachable"),
    ],
)
async def test_every_failure_has_one_of_five_names(
    openai_fake: FakeOpenAi, status: int, code: str
) -> None:
    openai_fake.fails = [(status, {"error": {"message": "whatever the provider said"}})]

    with pytest.raises(ProblemError) as failure:
        await openai_provider(openai_fake).generate(PROMPT, Batch)
    assert failure.value.code == code
    assert "whatever the provider said" not in (failure.value.detail or "")


async def test_an_unreachable_provider_is_unreachable(openai_fake: FakeOpenAi) -> None:
    openai_fake.offline = True

    with pytest.raises(ProblemError) as failure:
        await openai_provider(openai_fake).generate(PROMPT, Batch)
    assert failure.value.code == "llm_unreachable"


async def test_openai_lists_its_models(openai_fake: FakeOpenAi) -> None:
    openai_fake.models = ["gpt-b", "gpt-a", "gpt-a"]
    assert await openai_provider(openai_fake).list_models() == ["gpt-a", "gpt-b"]


async def test_a_wrong_key_cannot_be_used_to_list(openai_fake: FakeOpenAi) -> None:
    with pytest.raises(ProblemError) as failure:
        await openai_provider(openai_fake, api_key="wrong").list_models()
    assert failure.value.code == "llm_auth_failed"


# --- the connection test ------------------------------------------------------------


async def test_a_working_provider_tests_ok(openai_fake: FakeOpenAi) -> None:
    check = await openai_provider(openai_fake).test()
    assert check.ok
    assert check.server_name == "openai"


async def test_a_wrong_key_tests_unauthorized(openai_fake: FakeOpenAi) -> None:
    assert (await openai_provider(openai_fake, api_key="wrong").test()).health == "unauthorized"


async def test_an_unreachable_provider_tests_unreachable(openai_fake: FakeOpenAi) -> None:
    openai_fake.offline = True
    assert (await openai_provider(openai_fake).test()).health == "unreachable"


async def test_a_quota_shows_as_an_unexpected_answer(openai_fake: FakeOpenAi) -> None:
    openai_fake.models_status = 429
    assert (await openai_provider(openai_fake).test()).health == "unexpected_response"


async def test_a_provider_that_does_not_list_the_model_still_tests_ok(
    openai_fake: FakeOpenAi,
) -> None:
    openai_fake.models = ["something-else"]
    assert (await openai_provider(openai_fake).test()).ok


# --- Anthropic ----------------------------------------------------------------------


@pytest.fixture
def anthropic_fake() -> FakeAnthropic:
    """An Anthropic that calls the answer tool."""
    return FakeAnthropic(answers=[ONE_CARD])


def anthropic_provider(fake: FakeAnthropic, api_key: str = ANTHROPIC_KEY) -> AnthropicProvider:
    """A real Anthropic adapter talking to ``fake``."""
    return AnthropicProvider(api_key, "model-a", transport=fake.transport)


async def test_anthropic_answers_through_a_forced_tool_call(
    anthropic_fake: FakeAnthropic,
) -> None:
    found = await anthropic_provider(anthropic_fake).generate(PROMPT, Batch)

    assert [pick.title for pick in found.value.cards] == ["Arrival"]
    assert found.usage.input_tokens == 11
    body = body_of(anthropic_fake.requests[-1])
    assert body["tool_choice"] == {"type": "tool", "name": "answer"}
    assert body["tools"][0]["input_schema"]["required"] == ["cards"]
    assert body["max_tokens"] == 900
    assert body["system"] == "You pick films."
    # Anthropic's current API has no temperature; sending one is a hard error.
    assert "temperature" not in body


async def test_anthropic_answering_with_prose_is_invalid_output(
    anthropic_fake: FakeAnthropic,
) -> None:
    anthropic_fake.answers_with_text = True

    with pytest.raises(ProblemError) as failure:
        await anthropic_provider(anthropic_fake).generate(PROMPT, Batch)
    assert failure.value.code == "llm_invalid_output"


async def test_anthropic_lists_its_models(anthropic_fake: FakeAnthropic) -> None:
    anthropic_fake.models = ["claude-b", "claude-a"]
    assert await anthropic_provider(anthropic_fake).list_models() == ["claude-a", "claude-b"]


async def test_anthropic_maps_its_failures(anthropic_fake: FakeAnthropic) -> None:
    anthropic_fake.fails = [(429, {"type": "error", "error": {"type": "rate_limit_error"}})]
    with pytest.raises(ProblemError) as failure:
        await anthropic_provider(anthropic_fake).generate(PROMPT, Batch)
    assert failure.value.code == "llm_quota"

    anthropic_fake.offline = True
    assert (await anthropic_provider(anthropic_fake).test()).health == "unreachable"


async def test_anthropic_that_cannot_be_reached_at_all(
    anthropic_fake: FakeAnthropic,
) -> None:
    anthropic_fake.offline = True

    with pytest.raises(ProblemError) as failure:
        await anthropic_provider(anthropic_fake).generate(PROMPT, Batch)
    assert failure.value.code == "llm_unreachable"

    with pytest.raises(ProblemError) as listing:
        await anthropic_provider(anthropic_fake).list_models()
    assert listing.value.code == "llm_unreachable"


async def test_anthropic_refusing_a_listing_is_reported(anthropic_fake: FakeAnthropic) -> None:
    anthropic_fake.models_status = 403

    with pytest.raises(ProblemError) as failure:
        await anthropic_provider(anthropic_fake).list_models()
    assert failure.value.code == "llm_auth_failed"


async def test_anthropic_retries_once(anthropic_fake: FakeAnthropic) -> None:
    anthropic_fake.answers = ['{"cards": [{"year": "soon"}]}', ONE_CARD]
    found = await anthropic_provider(anthropic_fake).generate(PROMPT, Batch)
    assert found.retried


# --- Gemini -------------------------------------------------------------------------


@pytest.fixture
def gemini_fake() -> FakeGemini:
    """A Gemini that answers one card."""
    return FakeGemini(answers=[ONE_CARD])


def gemini_provider(fake: FakeGemini, api_key: str = GEMINI_KEY) -> GeminiProvider:
    """A real Gemini adapter talking to ``fake``."""
    return GeminiProvider(api_key, "model-a", transport=fake.transport)


async def test_gemini_generates_a_validated_object(gemini_fake: FakeGemini) -> None:
    found = await gemini_provider(gemini_fake).generate(PROMPT, Batch)

    assert [pick.title for pick in found.value.cards] == ["Arrival"]
    assert found.usage.output_tokens == 7
    body = body_of(gemini_fake.requests[-1])
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["systemInstruction"]["parts"][0]["text"] == "You pick films."


async def test_gemini_filtering_an_answer_away_is_invalid_output(
    gemini_fake: FakeGemini,
) -> None:
    gemini_fake.answers_empty = True

    with pytest.raises(ProblemError) as failure:
        await gemini_provider(gemini_fake).generate(PROMPT, Batch)
    assert failure.value.code == "llm_invalid_output"


async def test_gemini_lists_only_the_models_that_generate(gemini_fake: FakeGemini) -> None:
    gemini_fake.models = ["gemini-b", "gemini-a"]
    assert await gemini_provider(gemini_fake).list_models() == ["gemini-a", "gemini-b"]


async def test_gemini_maps_its_failures(gemini_fake: FakeGemini) -> None:
    assert (await gemini_provider(gemini_fake, api_key="wrong").test()).health == "unauthorized"

    gemini_fake.fails = [(429, {"error": {"code": 429, "message": "quota"}})]
    with pytest.raises(ProblemError) as failure:
        await gemini_provider(gemini_fake).generate(PROMPT, Batch)
    assert failure.value.code == "llm_quota"

    gemini_fake.offline = True
    assert (await gemini_provider(gemini_fake).test()).health == "unreachable"


async def test_gemini_that_cannot_be_reached_at_all(gemini_fake: FakeGemini) -> None:
    gemini_fake.offline = True

    with pytest.raises(ProblemError) as failure:
        await gemini_provider(gemini_fake).generate(PROMPT, Batch)
    assert failure.value.code == "llm_unreachable"

    with pytest.raises(ProblemError) as listing:
        await gemini_provider(gemini_fake).list_models()
    assert listing.value.code == "llm_unreachable"


async def test_gemini_refusing_a_listing_is_reported(gemini_fake: FakeGemini) -> None:
    gemini_fake.models_status = 404

    with pytest.raises(ProblemError) as failure:
        await gemini_provider(gemini_fake).list_models()
    assert failure.value.code == "llm_model_not_found"


async def test_gemini_retries_once(gemini_fake: FakeGemini) -> None:
    gemini_fake.answers = ["not json", ONE_CARD]
    assert (await gemini_provider(gemini_fake).generate(PROMPT, Batch)).retried


# --- Ollama -------------------------------------------------------------------------


@pytest.fixture
def ollama_fake() -> FakeOllama:
    """A local Ollama that answers one card."""
    return FakeOllama(answers=[ONE_CARD])


def ollama_provider(fake: FakeOllama, base_url: str = OLLAMA_URL) -> OllamaProvider:
    """A real Ollama adapter talking to ``fake``."""
    return OllamaProvider(base_url, "model-a", transport=fake.transport)


async def test_ollama_constrains_generation_with_the_schema(ollama_fake: FakeOllama) -> None:
    found = await ollama_provider(ollama_fake).generate(PROMPT, Batch)

    assert [pick.title for pick in found.value.cards] == ["Arrival"]
    assert found.usage.input_tokens == 11
    body = body_of(ollama_fake.requests[-1])
    assert body["format"]["required"] == ["cards"]
    assert body["stream"] is False
    assert body["options"] == {"num_predict": 900, "temperature": 0.8}


async def test_ollama_lists_what_it_has_pulled(ollama_fake: FakeOllama) -> None:
    ollama_fake.models = ["llama3:8b", "mistral:7b"]
    assert await ollama_provider(ollama_fake).list_models() == ["llama3:8b", "mistral:7b"]


async def test_an_ollama_that_is_not_running_is_unreachable(ollama_fake: FakeOllama) -> None:
    ollama_fake.offline = True
    assert (await ollama_provider(ollama_fake).test()).health == "unreachable"

    with pytest.raises(ProblemError) as failure:
        await ollama_provider(ollama_fake).generate(PROMPT, Batch)
    assert failure.value.code == "llm_unreachable"


async def test_an_ollama_without_that_model_says_so(ollama_fake: FakeOllama) -> None:
    ollama_fake.fails = [(404, {"error": "model not found"})]

    with pytest.raises(ProblemError) as failure:
        await ollama_provider(ollama_fake).generate(PROMPT, Batch)
    assert failure.value.code == "llm_model_not_found"


async def test_an_ollama_listing_that_fails_is_reported(ollama_fake: FakeOllama) -> None:
    ollama_fake.models_status = 500
    with pytest.raises(ProblemError) as failure:
        await ollama_provider(ollama_fake).list_models()
    assert failure.value.code == "llm_unreachable"


async def test_an_ollama_answer_with_no_message_is_invalid_output(
    ollama_fake: FakeOllama,
) -> None:
    ollama_fake.answers_empty = True
    with pytest.raises(ProblemError) as failure:
        await ollama_provider(ollama_fake).generate(PROMPT, Batch)
    assert failure.value.code == "llm_invalid_output"


async def test_an_ollama_that_is_not_one_is_unexpected(ollama_fake: FakeOllama) -> None:
    ollama_fake.fails = [(200, {"not": "a chat answer"})]
    with pytest.raises(ProblemError):
        await ollama_provider(ollama_fake).generate(PROMPT, Batch)


# --- the factory --------------------------------------------------------------------


def built(connection: LlmConnection) -> LlmProvider:
    """Build a provider the way the composition root does."""
    return llm_provider_factory()(connection)


def test_each_kind_gets_its_adapter() -> None:
    assert isinstance(built(LlmConnection("openai", api_key="k", model="m")), OpenAiProvider)
    assert isinstance(built(LlmConnection("anthropic", api_key="k", model="m")), AnthropicProvider)
    assert isinstance(built(LlmConnection("gemini", api_key="k", model="m")), GeminiProvider)
    assert isinstance(
        built(LlmConnection("ollama", base_url=OLLAMA_URL, model="m")), OllamaProvider
    )


def test_mistral_is_the_openai_adapter_at_mistrals_address() -> None:
    provider = built(LlmConnection("mistral", api_key="k", model="m"))
    assert isinstance(provider, OpenAiProvider)
    assert provider.base_url == MISTRAL_BASE_URL


def test_the_openai_kind_can_never_be_pointed_elsewhere() -> None:
    # A base URL left behind by an earlier provider must not redirect "openai".
    provider = built(LlmConnection("openai", api_key="k", base_url=COMPATIBLE_URL, model="m"))
    assert isinstance(provider, OpenAiProvider)
    assert "api.openai.com" in provider.base_url


def test_a_compatible_endpoint_goes_where_it_is_told() -> None:
    provider = built(
        LlmConnection("openai_compatible", api_key="k", base_url=COMPATIBLE_URL, model="m")
    )
    assert isinstance(provider, OpenAiProvider)
    assert provider.base_url == COMPATIBLE_URL


@pytest.mark.parametrize(
    "connection",
    [
        LlmConnection("openai", model="m"),
        LlmConnection("anthropic", model="m"),
        LlmConnection("gemini", model="m"),
        LlmConnection("mistral", model="m"),
        LlmConnection("openai_compatible", api_key="k", model="m"),
        LlmConnection("ollama", model="m"),
    ],
)
def test_a_connection_that_cannot_work_is_refused(connection: LlmConnection) -> None:
    with pytest.raises(ProblemError) as failure:
        built(connection)
    assert failure.value.code == "validation_error"


def test_an_ollama_needs_no_key() -> None:
    assert built(LlmConnection("ollama", base_url=OLLAMA_URL, model="m")) is not None


def test_a_compatible_endpoint_needs_no_key_either() -> None:
    provider = built(LlmConnection("openai_compatible", base_url=COMPATIBLE_URL, model="m"))
    assert isinstance(provider, OpenAiProvider)
