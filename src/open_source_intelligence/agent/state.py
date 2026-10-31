"""Bounded Agent state shared across the research graph nodes.

The shape mirrors `ARCHITECTURE.md` §8.2. Scalar fields are replaced by each node, while
plan, evidence, and section lists accumulate through the `operator.add` reducer so a node
only ever returns its own contribution.
"""

from __future__ import annotations

import operator
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, TypedDict

from pydantic import Field

from open_source_intelligence.agent.llm.messages import ChatMessage, ModelToolCall
from open_source_intelligence.agent.tools.contracts import SectionRef, StrictModel

AgentRunStatus = Literal["pending", "running", "needs_review", "completed", "failed"]
ResearchIntent = Literal["profile", "metrics", "prior_reports", "content"]


class ResearchTask(StrictModel):
    """One planned research action recorded in the run trail."""

    task_index: int
    intent: ResearchIntent
    repo_id: int | None = None


class ToolObservation(StrictModel):
    """One raw tool outcome carried from the tool node into the observe node."""

    tool_name: str
    repo_id: int | None = None
    ok: bool
    result_count: int
    truncated: bool
    error_code: str | None = None
    payload: Any = None
    guard_triggered: bool = False


class EvidenceRef(StrictModel):
    """One tool observation summarized for the run trail."""

    tool_name: str
    repo_id: int | None = None
    ok: bool
    result_count: int
    truncated: bool
    error_code: str | None = None
    summary: str = ""
    guard_triggered: bool = False


class VerificationResult(StrictModel):
    """Outcome of the evidence-coverage check that gates report writing."""

    sufficient: bool
    reason: str
    covered_repo_ids: list[int]
    missing_repo_ids: list[int]
    content_repo_ids: list[int] = Field(default_factory=list)
    has_prior_baseline: bool = False
    security_risk: bool = False


class AgentState(TypedDict, total=False):
    """State threaded through the research graph."""

    run_id: int
    topic_id: int
    window_start: datetime
    window_end: datetime
    candidate_repo_ids: list[int]
    prompt_version: str
    max_loops: int
    max_model_calls: int
    max_tool_calls: int
    max_tokens: int
    max_cost: Decimal
    research_plan: Annotated[list[ResearchTask], operator.add]
    evidence: Annotated[list[EvidenceRef], operator.add]
    draft_sections: Annotated[list[SectionRef], operator.add]
    messages: Annotated[list[ChatMessage], operator.add]
    last_observations: list[ToolObservation]
    pending_tool_calls: list[ModelToolCall]
    verification: VerificationResult | None
    loop_count: int
    tool_call_count: int
    model_call_count: int
    prompt_tokens: int
    completion_tokens: int
    estimated_cost: Decimal
    budget_degraded: bool
    guard_triggered: bool
    status: str
    error_code: str | None
    error_message: str | None
    incomplete: bool
    readme_nudged: bool
