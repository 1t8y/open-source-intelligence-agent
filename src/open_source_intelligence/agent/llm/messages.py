"""Provider-neutral chat messages, tool schemas, and model turns."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ChatRole = Literal["system", "user", "assistant", "tool"]

# Context blocks use these tags so every provider reads the same machine-parsable facts.
CANDIDATE_TAG = "candidate_repos"
TOPIC_TAG = "topic_id"
WINDOW_TAG = "research_window"


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
