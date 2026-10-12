"""Provider-neutral model client contract."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .messages import ChatMessage, ModelTurn, ToolSchema


@runtime_checkable
class ModelClient(Protocol):
    """Minimal synchronous chat contract implemented by every provider adapter."""

    @property
    def provider(self) -> str:
        """Return the provider identifier recorded on the Agent config."""
        ...

    @property
    def model(self) -> str:
        """Return the concrete model identifier used for billing and audit."""
        ...

    def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema],
    ) -> ModelTurn:
        """Return one assistant turn for the given conversation and tool set."""
        ...
