"""Execution profiles: the ablation switches the evaluation runner flips.

`docs/evaluation.md` §8 requires the ablations to answer "what breaks when a capability is
removed", and the only way that answer means anything is if the *same* code path runs with one
capability switched off. So an ablation is not a reimplementation: it is a profile that the
real research runtime reads, and a production run always uses `AGENT_FULL`.

Two switches cover all three documented ablations:

* `run_verifier=False` removes the evidence-coverage gate (`agent_no_verifier`).
* `disabled_tools` removes read tools from the whitelist the run may call
  (`agent_no_tools` drops every read tool, `agent_no_history` drops only
  `search_prior_reports`).

The draft-writing tool is deliberately *not* disableable: an ablation that produced no draft
at all would measure nothing, so the runtime always keeps it when the agent config enabled it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from open_source_intelligence.agent.tools.registry import WHITELIST

#: The only tool that writes; every profile keeps it so an ablation still produces a draft.
WRITE_TOOL: Final[str] = "save_report_section"

#: Every read-only tool in the whitelist, in whitelist order.
READ_TOOLS: Final[tuple[str, ...]] = tuple(name for name in WHITELIST if name != WRITE_TOOL)


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    """The capability set one research run is allowed to use."""

    name: str
    run_verifier: bool = True
    disabled_tools: frozenset[str] = field(default_factory=frozenset)

    def allows(self, tool_name: str) -> bool:
        """Return whether this profile lets the run call one tool."""
        return tool_name not in self.disabled_tools


#: The production profile: nothing is switched off.
AGENT_FULL: Final[ExecutionProfile] = ExecutionProfile(name="agent_full")

#: No verifier: the coverage gate that normally blocks writing is removed.
PROFILE_NO_VERIFIER: Final[ExecutionProfile] = ExecutionProfile(
    name="agent_no_verifier",
    run_verifier=False,
)

#: No read tools: the draft is written from the candidate context alone.
PROFILE_NO_TOOLS: Final[ExecutionProfile] = ExecutionProfile(
    name="agent_no_tools",
    disabled_tools=frozenset(READ_TOOLS),
)

#: No history: the run keeps its tools but may not consult prior reports.
PROFILE_NO_HISTORY: Final[ExecutionProfile] = ExecutionProfile(
    name="agent_no_history",
    disabled_tools=frozenset({"search_prior_reports"}),
)

#: The profiles used by the three ablation targets, keyed by their `target_type` value.
ABLATION_PROFILES: Final[dict[str, ExecutionProfile]] = {
    PROFILE_NO_VERIFIER.name: PROFILE_NO_VERIFIER,
    PROFILE_NO_TOOLS.name: PROFILE_NO_TOOLS,
    PROFILE_NO_HISTORY.name: PROFILE_NO_HISTORY,
}


def profile_for_target(target_type: str) -> ExecutionProfile | None:
    """Return the profile an agent target type uses, or None when it is not an agent target."""
    if target_type == AGENT_FULL.name:
        return AGENT_FULL
    return ABLATION_PROFILES.get(target_type)
