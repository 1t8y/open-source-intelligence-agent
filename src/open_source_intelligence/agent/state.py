"""研究图各节点共享的、有上界的 Agent 状态。

结构对齐 `ARCHITECTURE.md` §8.2。标量字段由各节点整体替换，而计划、证据
与章节列表通过 `operator.add` reducer 累加，因此每个节点只需返回自己的
那份贡献。
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
    """记录在运行轨迹里的一条已计划研究动作。"""

    task_index: int
    intent: ResearchIntent
    repo_id: int | None = None


class ToolObservation(StrictModel):
    """从 `tool` 节点带入 `observe` 节点的一次原始工具结果。"""

    tool_name: str
    repo_id: int | None = None
    ok: bool
    result_count: int
    truncated: bool
    error_code: str | None = None
    payload: Any = None
    guard_triggered: bool = False


class EvidenceRef(StrictModel):
    """为运行轨迹汇总的一条工具观测。"""

    tool_name: str
    repo_id: int | None = None
    ok: bool
    result_count: int
    truncated: bool
    error_code: str | None = None
    summary: str = ""
    guard_triggered: bool = False


class VerificationResult(StrictModel):
    """证据覆盖度检查的结果，这道闸决定报告能否开写。"""

    sufficient: bool
    reason: str
    covered_repo_ids: list[int]
    missing_repo_ids: list[int]
    content_repo_ids: list[int] = Field(default_factory=list)
    has_prior_baseline: bool = False
    security_risk: bool = False


class AgentState(TypedDict, total=False):
    """贯穿研究图的状态。"""

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
