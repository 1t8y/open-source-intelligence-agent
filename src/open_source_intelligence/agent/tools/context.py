"""Runtime-injected authorization and budget scope for one Agent run."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Trusted context the model cannot forge through tool arguments."""

    agent_run_id: int
    topic_id: int
    authorized_topic_ids: frozenset[int]
    allowed_tools: tuple[str, ...]
    max_tool_calls: int
    tool_call_count: int = 0
    #: Ids of the `tool_calls` rows written so far, in order. The registry appends here as it
    #: records; the graph drains the tail into the step it is about to write, because the step row
    #: does not exist yet when the calls run. Mutating a list on a frozen dataclass is fine — what
    #: frozen forbids is rebinding the attribute.
    recorded_call_ids: list[int] = field(default_factory=list)

    def can_access_topic(self, topic_id: int) -> bool:
        """Return whether the run may read resources of the given topic."""
        return topic_id in self.authorized_topic_ids
