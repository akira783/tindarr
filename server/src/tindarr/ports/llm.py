"""The AI provider port: ask a model for one JSON object and get a validated one back.

Everything a model returns is **untrusted input** (the security model, section 6). The
port's shape says so: ``generate`` never returns text, only an instance of the Pydantic
schema the caller passed. Anything the model wrote that the schema does not describe is
dropped before it reaches the domain, and what survives is only ever used as data —
titles to look up on TMDb, sentences to display as plain text. No provider chooses a
tool, reaches a secret or sees another user's data.

The six provider kinds of the HTTP contract are covered by four adapters: the OpenAI SDK
serves OpenAI, Mistral and any OpenAI-compatible endpoint (they differ by base URL and
by nothing else), and Anthropic, Gemini and Ollama have their own. ``capabilities`` says
how strictly a provider can be held to a schema, which is what the shared layer uses to
decide between a real JSON schema, plain JSON mode and "ask nicely, then repair".
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel

from tindarr.ports.connectors import ConnectionCheck

#: The provider kinds the contract's ``LlmProviderKind`` lists.
type LlmProviderKind = Literal[
    "openai", "anthropic", "gemini", "mistral", "openai_compatible", "ollama"
]
#: How tightly the provider can be held to a JSON schema, strongest first.
type StructuredMode = Literal["json_schema", "json_mode", "text"]
#: The contract's ``reasoning_effort``.
type ReasoningEffort = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class LlmCapabilities:
    """What a provider can be asked for."""

    structured: StructuredMode = "json_schema"
    #: Whether ``reasoning_effort`` may be sent at all (most endpoints reject it).
    reasoning_effort: bool = False
    #: Whether the provider still has a temperature. Anthropic's current API has none:
    #: variety is asked for in the prompt there, not turned up with a dial.
    temperature: bool = True


@dataclass(frozen=True, slots=True)
class Prompt:
    """One request to a model: a system line, a user message, and the dials.

    There is no conversation. Every call is one shot, so nothing a model said earlier
    can steer a later batch, and a retry re-sends the original prompt rather than
    handing the model its own broken output back.
    """

    instructions: str
    message: str
    temperature: float | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class LlmUsage:
    """Tokens one call cost, for the administrator's usage page (step 4)."""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Generation[T: BaseModel]:
    """A validated answer, with what it cost and which model produced it."""

    value: T
    model: str
    usage: LlmUsage = LlmUsage()
    #: True when the first answer failed validation and the single retry rescued it.
    retried: bool = False


@dataclass(frozen=True, slots=True)
class LlmConnection:
    """Everything an adapter needs to talk to the configured AI provider.

    ``base_url`` is required for ``openai_compatible`` and ``ollama`` and ignored for
    the three that have one address; ``api_key`` is required for everything but
    ``ollama``, which has no accounts.
    """

    kind: LlmProviderKind
    api_key: str = ""
    base_url: str | None = None
    model: str = ""
    reasoning_effort: ReasoningEffort | None = None


class LlmProvider(Protocol):
    """One AI provider, reduced to what the swipe engine needs."""

    kind: LlmProviderKind
    capabilities: LlmCapabilities

    async def test(self) -> ConnectionCheck:
        """Check the credentials, without raising for a remote failure."""
        ...

    async def list_models(self) -> list[str]:
        """Return the model ids this provider offers, so none is hard-coded."""
        ...

    async def generate[T: BaseModel](self, prompt: Prompt, schema: type[T]) -> Generation[T]:
        """Ask for one object of ``schema`` and return it validated."""
        ...


class LlmProviderFactory(Protocol):
    """Builds the adapter for a connection. Only ``tindarr.main`` implements it."""

    def __call__(self, connection: LlmConnection) -> LlmProvider:
        """Return an adapter talking to ``connection``."""
        ...
