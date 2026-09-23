"""Fake AI providers, behind the transport each SDK really uses.

The adapters are driven through the **real** SDKs: the requests these fakes receive are
the bodies OpenAI, Anthropic and Gemini would receive, so a test can assert that the
JSON schema travelled, that the retry re-sent the original prompt, and that no key ever
appeared where it should not.

``google-genai`` carries its own copy of ``httpx``, so its fake speaks that flavour;
everything else speaks ``httpx2``, which is Tindarr's own.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast

import httpx
import httpx2

OPENAI_KEY: Final = "sk-test-0123456789"
ANTHROPIC_KEY: Final = "sk-ant-test-0123456789"
GEMINI_KEY: Final = "gemini-test-0123456789"
OLLAMA_URL: Final = "http://ollama.lan:11434"
COMPATIBLE_URL: Final = "http://gateway.lan:4000/v1"


def body_of(request: httpx2.Request | httpx.Request) -> Mapping[str, Any]:
    """Return the JSON body a request carried."""
    try:
        payload: Any = json.loads(request.content or b"{}")
    except ValueError:
        return {}
    return cast("Mapping[str, Any]", payload) if isinstance(payload, dict) else {}


@dataclass
class _Provider:
    """What every fake provider has: answers to hand out, and a record of the calls."""

    #: Answers for successive completions; the last one repeats once the list runs out.
    answers: list[str] = field(default_factory=lambda: ['{"value": "ok"}'])
    models: list[str] = field(default_factory=lambda: ["model-a", "model-b"])
    #: ``(status, payload)`` forced on the next completion, then cleared.
    fails: list[tuple[int, dict[str, Any]]] = field(
        default_factory=list[tuple[int, dict[str, Any]]]
    )
    #: Forced on every model listing.
    models_status: int = 200
    offline: bool = False
    calls: int = 0

    def next_answer(self) -> str:
        """The answer for this completion."""
        index = min(self.calls, len(self.answers) - 1)
        self.calls += 1
        return self.answers[index]

    def next_failure(self) -> tuple[int, dict[str, Any]] | None:
        """The failure forced on this completion, if one was."""
        return self.fails.pop(0) if self.fails else None


@dataclass
class FakeOpenAi(_Provider):
    """An OpenAI-compatible endpoint."""

    api_key: str = OPENAI_KEY
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    @property
    def transport(self) -> httpx2.MockTransport:
        """The transport the SDK talks through."""
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one request."""
        self.requests.append(request)
        if self.offline:
            raise httpx2.ConnectError("the AI provider is unreachable")
        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return httpx2.Response(401, json={"error": {"message": "Invalid API key"}})
        if request.url.path.endswith("/models"):
            if self.models_status != 200:
                return httpx2.Response(self.models_status, json={"error": {"message": "no"}})
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"id": name, "object": "model", "created": 0, "owned_by": "test"}
                        for name in self.models
                    ],
                },
            )
        forced = self.next_failure()
        if forced is not None:
            return httpx2.Response(forced[0], json=forced[1])
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "model-a",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.next_answer()},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )


@dataclass
class FakeAnthropic(_Provider):
    """Anthropic's messages API."""

    api_key: str = ANTHROPIC_KEY
    #: Set to answer with prose instead of the forced tool call.
    answers_with_text: bool = False
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    @property
    def transport(self) -> httpx2.MockTransport:
        """The transport the SDK talks through."""
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one request."""
        self.requests.append(request)
        if self.offline:
            raise httpx2.ConnectError("the AI provider is unreachable")
        if request.headers.get("x-api-key") != self.api_key:
            return httpx2.Response(
                401, json={"type": "error", "error": {"type": "authentication_error"}}
            )
        if request.url.path.endswith("/models"):
            if self.models_status != 200:
                return httpx2.Response(self.models_status, json={"error": {"type": "no"}})
            return httpx2.Response(
                200,
                json={
                    "data": [
                        {
                            "id": name,
                            "type": "model",
                            "display_name": name,
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                        for name in self.models
                    ],
                    "has_more": False,
                    "first_id": None,
                    "last_id": None,
                },
            )
        forced = self.next_failure()
        if forced is not None:
            return httpx2.Response(forced[0], json=forced[1])
        answer = self.next_answer()
        content: list[dict[str, Any]] = (
            [{"type": "text", "text": answer}]
            if self.answers_with_text
            else [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "answer",
                    "input": json.loads(answer),
                }
            ]
        )
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "model-a",
                "content": content,
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 11, "output_tokens": 7},
            },
        )


@dataclass
class FakeGemini(_Provider):
    """Google's generative language API, in its own flavour of httpx."""

    api_key: str = GEMINI_KEY
    #: Set to answer with no candidate at all (a safety filter).
    answers_empty: bool = False
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])

    @property
    def transport(self) -> httpx.MockTransport:
        """The transport ``google-genai`` talks through."""
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request."""
        self.requests.append(request)
        if self.offline:
            raise httpx.ConnectError("the AI provider is unreachable")
        if request.headers.get("x-goog-api-key") != self.api_key:
            return httpx.Response(
                401, json={"error": {"code": 401, "message": "API key not valid"}}
            )
        if request.url.path.endswith(":generateContent"):
            forced = self.next_failure()
            if forced is not None:
                return httpx.Response(forced[0], json=forced[1])
            if self.answers_empty:
                return httpx.Response(200, json={"candidates": []})
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": self.next_answer()}],
                                "role": "model",
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7},
                },
            )
        if self.models_status != 200:
            return httpx.Response(
                self.models_status, json={"error": {"code": self.models_status, "message": "no"}}
            )
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": f"models/{name}", "supportedGenerationMethods": ["generateContent"]}
                    for name in self.models
                ]
                + [{"name": "models/embed-1", "supportedGenerationMethods": ["embedContent"]}]
            },
        )


@dataclass
class FakeOllama(_Provider):
    """A local Ollama."""

    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])
    #: Set to answer a chat with no message at all.
    answers_empty: bool = False

    @property
    def transport(self) -> httpx2.MockTransport:
        """The transport the adapter talks through."""
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one request."""
        self.requests.append(request)
        if self.offline:
            raise httpx2.ConnectError("ollama is unreachable")
        if request.url.path == "/api/tags":
            if self.models_status != 200:
                return httpx2.Response(self.models_status, json={"error": "no"})
            return httpx2.Response(
                200, json={"models": [{"model": name, "name": name} for name in self.models]}
            )
        forced = self.next_failure()
        if forced is not None:
            return httpx2.Response(forced[0], json=forced[1])
        if self.answers_empty:
            return httpx2.Response(200, json={"done": True})
        return httpx2.Response(
            200,
            json={
                "model": "model-a",
                "message": {"role": "assistant", "content": self.next_answer()},
                "done": True,
                "prompt_eval_count": 11,
                "eval_count": 7,
            },
        )
