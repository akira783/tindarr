"""Turning whatever a model wrote into an instance of a schema, or into a problem.

Three things happen here, in order, and each is deliberately small.

**Repair.** A model asked for JSON answers with JSON inside a code fence, or with a
sentence before it, or — the one the fork hit often enough to name — with
``"Blade Runner" (1982)`` where a single string belonged. Those three are fixed. Nothing
else is: a repair that guesses turns invalid output into *wrong* output, which is worse,
because invalid output is caught and wrong output is served to somebody as a
recommendation.

**Validation.** The repaired text is parsed and validated against the caller's Pydantic
model. That model is the only thing that reaches the domain, so a field the schema does
not describe — an instruction, a URL, an extra "system" key — never exists past this
point.

**One retry.** A schema failure re-sends the *original* prompt with one extra
instruction. The model's broken answer is never fed back: it is untrusted text, and
quoting it into the next prompt is exactly the path a prompt injection would take.
Two attempts, then ``llm_invalid_output``.
"""

import json
import logging
import re
from collections.abc import Iterable
from http import HTTPStatus
from typing import Any, Final, cast

from pydantic import BaseModel, ValidationError

from tindarr.core.errors import ProblemError
from tindarr.ports import problems
from tindarr.ports.connectors import ConnectorHealth

#: What the retry adds to the system instructions. It says nothing about the previous
#: answer beyond that it did not fit: there is nothing to quote back.
RETRY_INSTRUCTION: Final = (
    "Your previous answer did not match the required JSON schema. Answer with strictly "
    "valid JSON matching the schema: no code fence, no comment, no extra field, no text "
    "before or after the object."
)
#: How much of an answer is written to a debug log; never more, and never above debug.
_PREVIEW: Final = 200
#: How many model ids one provider may put in front of an administrator, and how long
#: each may be. A catalogue is a few dozen short strings; an endpoint somebody typed
#: answering with a hundred thousand of them is not a catalogue.
MAX_MODELS: Final = 500
MAX_MODEL_ID: Final = 128
#: ``"Title" (1982)`` -> ``"Title (1982)"``.
_QUALIFIER: Final = re.compile(r'"([^"]*?)"\s+(\([^)]*?\))')

logger = logging.getLogger(__name__)


class InvalidOutputError(Exception):
    """The model's answer did not parse, or did not fit the schema."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def strip_fences(text: str) -> str:
    """Remove a leading ```` ```json ```` fence and a trailing ```` ``` ````."""
    stripped = text.strip()
    stripped = stripped.removeprefix("```json").removeprefix("```jsonc")
    stripped = stripped.removeprefix("```")
    return stripped.removesuffix("```").strip()


def repair_title_qualifiers(text: str) -> str:
    """Join ``"Blade Runner" (1982)`` back into one string.

    Models split a title and its year this way often enough that the fork carries a
    regular expression for it. It is blunt — it will also join two adjacent strings that
    happened to look like that — but it only ever runs on text that is *about* to be
    parsed as JSON, and a value that was already valid JSON does not contain the
    sequence.
    """
    return _QUALIFIER.sub(r'"\1 \2"', text)


def extract_json(text: str) -> str:
    """Return the first complete JSON object or array in ``text``.

    From the first ``{`` or ``[`` to the last matching closer, which handles both the
    sentence before ("Here is your JSON:") and the one after. Nested braces need no
    counting: the last closer of the outermost opener is the end of the document.
    """
    starts = [index for index in (text.find("{"), text.find("[")) if index != -1]
    if not starts:
        return text.strip()
    start = min(starts)
    closer = "}" if text[start] == "{" else "]"
    end = text.rfind(closer)
    return (text[start : end + 1] if end >= start else text[start:]).strip()


def repair(text: str) -> str:
    """Run the three repairs, in the order that makes each one's job possible."""
    return extract_json(repair_title_qualifiers(strip_fences(text)))


def _list_field(schema: type[BaseModel]) -> str | None:
    """Return the name of the schema's one list field, when it has exactly one."""
    names = [
        name
        for name, info in schema.model_fields.items()
        if str(info.annotation).startswith(("list[", "typing.List["))
    ]
    return names[0] if len(names) == 1 else None


def reshape(parsed: object, schema: type[BaseModel]) -> object:
    """Put a bare array back inside the object the schema expects.

    A model asked for ``{"cards": [...]}`` answers with ``[...]``, or with
    ``{"results": [...]}``, often enough to be worth handling rather than retrying: the
    content is right and only the envelope is wrong. This only ever applies to a schema
    whose shape leaves no doubt — exactly one list field — so nothing is guessed.
    """
    field = _list_field(schema)
    if field is None:
        return parsed
    if isinstance(parsed, list):
        return {field: cast("list[object]", parsed)}
    if not isinstance(parsed, dict):
        return parsed
    payload = cast("dict[str, Any]", parsed)
    if field in payload:
        return payload
    lists = [key for key, value in payload.items() if isinstance(value, list)]
    if len(lists) != 1:
        return payload
    renamed = dict(payload)
    renamed[field] = renamed.pop(lists[0])
    return renamed


def parse_structured[T: BaseModel](text: str, schema: type[T]) -> T:
    """Repair, parse and validate an answer. Raises ``InvalidOutputError``."""
    content = repair(text)
    try:
        parsed: object = json.loads(content)
    except json.JSONDecodeError as failure:
        _log_bad_answer("the answer is not JSON", content, str(failure))
        raise InvalidOutputError("not_json") from None
    try:
        return schema.model_validate(reshape(parsed, schema))
    except ValidationError as failure:
        _log_bad_answer(
            "the answer does not fit the schema",
            content,
            "; ".join(
                str(item["msg"]) for item in failure.errors(include_input=False, include_url=False)
            ),
        )
        raise InvalidOutputError("schema_mismatch") from None


def _log_bad_answer(message: str, content: str, reason: str) -> None:
    logger.warning(message, extra={"llm": "invalid_output", "reason": reason[:_PREVIEW]})
    # The answer itself is user-facing content and untrusted text; debug only.
    logger.debug("the model answered", extra={"preview": content[:_PREVIEW].replace("\n", " ")})


def model_ids(names: Iterable[str | None]) -> list[str]:
    """Return a provider's model ids, reduced to what a list of ids can be.

    The console shows these and an administrator picks one, so what comes back from an
    address they typed is treated like every other remote answer: printable characters
    only, a length a model id can plausibly have, no duplicates, and a bound on how many
    (the security model, section 7 — a model listing returns ids, not a way of reading
    something else back out).
    """
    kept: set[str] = set()
    for name in names:
        if name is None:
            continue
        cleaned = "".join(character for character in name.strip() if character.isprintable())
        if cleaned and len(cleaned) <= MAX_MODEL_ID:
            kept.add(cleaned)
    return sorted(kept)[:MAX_MODELS]


def json_schema_of(schema: type[BaseModel]) -> dict[str, Any]:
    """Return the schema as JSON Schema, tightened the way strict modes require.

    OpenAI's strict structured outputs refuse a schema that allows extra properties or
    leaves one optional, and the other providers do not mind either constraint, so one
    shape is produced for all of them rather than one per provider. It is also the
    shape that matches what validation will do a moment later: pydantic drops what the
    model is told not to send anyway.
    """
    return _tighten(schema.model_json_schema())


def _tighten(node: object) -> Any:
    if isinstance(node, list):
        return [_tighten(item) for item in cast("list[object]", node)]
    if not isinstance(node, dict):
        return node
    found = {key: _tighten(value) for key, value in cast("dict[str, object]", node).items()}
    properties = found.get("properties")
    if isinstance(properties, dict):
        found["additionalProperties"] = False
        found["required"] = list(cast("dict[str, object]", properties))
    return found


def problem_for(status: int | None, *, connection: bool = False) -> ProblemError:
    """Map an AI provider's failure to one of the contract's five codes.

    The provider's own message never travels: an administrator is told what to change
    (the key, the model, the address, the plan), which is what they can act on, and
    nothing a remote service wrote is reflected back.
    """
    if connection or status is None:
        return problems.llm_unreachable()
    if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        return problems.llm_auth_failed()
    if status == HTTPStatus.NOT_FOUND:
        return problems.llm_model_not_found()
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        return problems.llm_quota()
    return problems.llm_unreachable()


def health_for(problem: ProblemError) -> ConnectorHealth:
    """Return the coarse connector health a mapped problem corresponds to."""
    if problem.code == "llm_auth_failed":
        return "unauthorized"
    if problem.code == "llm_unreachable":
        return "unreachable"
    return "unexpected_response"


def messages_of(instructions: str, retry_hint: str | None) -> str:
    """Return the system text of one call: the prompt's own, plus the retry's if any."""
    return instructions if retry_hint is None else f"{instructions}\n\n{retry_hint}"
