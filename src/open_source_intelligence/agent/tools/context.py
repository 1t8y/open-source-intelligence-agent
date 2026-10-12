"""Runtime-injected authorization and budget scope for one Agent run."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Trusted context the model cannot forge through tool arguments."""

    agent_run_id: int
    topic_id: int
    authorized_topic_ids: frozenset[int]
    allowed_tools: tuple[str, ...]
    max_tool_calls: int
    tool_call_count: int = 0

    def can_access_topic(self, topic_id: int) -> bool:
        """Return whether the run may read resources of the given topic."""
        return topic_id in self.authorized_topic_ids
