"""The AI providers, and the one layer that makes them interchangeable.

Six provider kinds, four adapters: the OpenAI SDK serves OpenAI, Mistral and any
OpenAI-compatible endpoint, because those differ by base URL and by nothing else, while
Anthropic, Gemini and Ollama each have an API of their own.

What they share is ``structured``: JSON extraction and repair, validation against a
Pydantic schema, one retry, and the five stable problem codes the contract documents.
That layer exists because the interesting failures are not the providers' differences
but their agreements — every one of them will, sooner or later, wrap its JSON in a code
fence, add a field nobody asked for, or stop halfway through an object.

Nothing a model returns is trusted. ``generate`` hands back an instance of the caller's
schema and never text, so anything outside the schema is gone before the domain sees it
(the security model, section 6).
"""

from tindarr.adapters.llm.factory import llm_provider_factory
from tindarr.adapters.llm.structured import (
    InvalidOutputError,
    extract_json,
    json_schema_of,
    parse_structured,
    repair_title_qualifiers,
    strip_fences,
)

__all__ = [
    "InvalidOutputError",
    "extract_json",
    "json_schema_of",
    "llm_provider_factory",
    "parse_structured",
    "repair_title_qualifiers",
    "strip_fences",
]
