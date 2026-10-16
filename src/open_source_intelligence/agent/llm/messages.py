"""Provider-neutral chat messages, tool schemas, and model turns."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

ChatRole = Literal["system", "user", "assistant", "tool"]

# Context blocks use these tags so every provider reads the same machine-parsable facts.
CANDIDATE_TAG = "candidate_repos"
TOPIC_TAG = "topic_id"
WINDOW_TAG = "research_window"

#: The top-level key that marks a tool observation as a failure rather than a result. Both the
#: writer (`agent/graph.py`, which renders the observation the model reads) and the reader
#: (`agent/llm/offline.py:_collect_evidence`, which rebuilds the research snapshot from the
#: conversation) go through the two functions below, so the shape has one definition.
TOOL_ERROR_FIELD: Final[str] = "error"


def tool_error_payload(code: str, message: str) -> dict[str, Any]:
    """Return the observation a failed tool call sends back to the model.

    A failure is rendered as an object rather than as text because the model has to be able to
    act on the code - `TOOL_NOT_FOUND` on one repository means "try another", while
    `TOOL_ACCESS_DENIED` means "that one is not yours to read". The consequence, and the reason
    this lives beside its predicate, is that a failure and a success are both JSON objects: a
    consumer that only asks "is this a dict?" cannot tell them apart, and will record a failed
    read as evidence that a document was read.
    """
    return {TOOL_ERROR_FIELD: {"code": code, "message": message}}


def is_tool_error_payload(payload: Any) -> bool:
    """Return whether an observation is the failure shell rather than a tool's own data.

    Deliberately structural rather than type-based: the conversation is the only thing the
    offline adapter can see, so "did this call succeed" has to be answered from the recorded
    message. A tool whose payload happens to be a dict full of fields is a result; the failure
    shell is a dict whose only content is an `error` object carrying a `code`.
    """
    if not isinstance(payload, dict):
        return False
    error = payload.get(TOOL_ERROR_FIELD)
    return isinstance(error, dict) and "code" in error


class ModelToolCall(BaseModel):
    """A tool invocation requested by the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolSchema(BaseModel):
    """JSON-schema view of one whitelisted tool offered to the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    parameters: dict[str, Any]


class ChatMessage(BaseModel):
    """One normalized conversation message shared by every provider adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: ChatRole
    content: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


class ModelTurn(BaseModel):
    """One model response carrying free text, requested tool calls, or both."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: Decimal = Decimal("0")

    @property
    def has_tool_calls(self) -> bool:
        """Return whether the turn asked to call at least one tool."""
        return bool(self.tool_calls)

    @property
    def total_tokens(self) -> int:
        """Return the summed prompt and completion token count."""
        return self.prompt_tokens + self.completion_tokens
