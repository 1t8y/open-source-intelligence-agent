"""Stable error codes recorded on `agent_runs.error_code`."""

from __future__ import annotations

AGENT_NO_CANDIDATES = "AGENT_NO_CANDIDATES"
AGENT_MODEL_UNAVAILABLE = "AGENT_MODEL_UNAVAILABLE"
AGENT_DRAFT_INVALID = "AGENT_DRAFT_INVALID"
AGENT_BUDGET_EXCEEDED = "AGENT_BUDGET_EXCEEDED"
AGENT_EVIDENCE_INCOMPLETE = "AGENT_EVIDENCE_INCOMPLETE"
AGENT_WRITE_FAILED = "AGENT_WRITE_FAILED"
AGENT_INTERNAL_ERROR = "AGENT_INTERNAL_ERROR"
AGENT_PROMPT_INJECTION = "AGENT_PROMPT_INJECTION"

AGENT_ERROR_CODES = (
    AGENT_NO_CANDIDATES,
    AGENT_MODEL_UNAVAILABLE,
    AGENT_DRAFT_INVALID,
    AGENT_BUDGET_EXCEEDED,
    AGENT_EVIDENCE_INCOMPLETE,
    AGENT_WRITE_FAILED,
    AGENT_INTERNAL_ERROR,
    AGENT_PROMPT_INJECTION,
)

#: Error codes that mean "a human should look at this" rather than "the run broke".
#:
#: This lives next to the codes rather than in `agent/runtime.py` because both the runtime (to
#: decide the terminal status) and the graph (to stop after a fatal upstream failure) need it,
#: and the graph cannot import the runtime: the runtime is what builds the graph.
REVIEW_REQUIRED_CODES = frozenset(
    {
        AGENT_BUDGET_EXCEEDED,
        AGENT_EVIDENCE_INCOMPLETE,
        AGENT_NO_CANDIDATES,
        AGENT_PROMPT_INJECTION,
        AGENT_WRITE_FAILED,
    }
)
