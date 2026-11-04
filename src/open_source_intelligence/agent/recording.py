"""持久化 Agent 运行轨迹：每个节点一条有序步骤，外加实时计数器。

每条步骤都会立即提交，因此运行中途崩溃时，仍会留下「运行走到过这里」的证据。
摘要在入库之前先做脱敏（INV-8）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from open_source_intelligence.core.logging import redact_text
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.agent import AgentRun, AgentStep, ToolCall

StepType = Literal["plan", "tool", "observe", "verify", "write"]

#: 步骤摘要用于诊断而非存储载荷，因此有长度上限。
STEP_SUMMARY_CHARS = 1000


def digest_text(value: str) -> str:
    """返回某条步骤输入或输出的稳定 SHA-256 摘要。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class StepRecorder:
    """追加有序的 `agent_steps` 行，并让所属运行行保持同步。"""

    def __init__(self, session: Session, agent_run: AgentRun) -> None:
        self._session = session
        self._agent_run = agent_run
        self._next_index = self._current_max_index() + 1

    def _current_max_index(self) -> int:
        """返回本次运行已存储的最高步骤序号。"""
        current = self._session.scalar(
            select(func.max(AgentStep.step_index)).where(
                AgentStep.agent_run_id == self._agent_run.id
            )
        )
        return int(current) if current is not None else 0

    def mark_running(self) -> None:
        """把运行标记为进行中，并记下开始时间。"""
        self._agent_run.status = "running"
        self._agent_run.started_at = utc_now()
        self._session.commit()

    def record(
        self,
        *,
        step_type: StepType,
        status: str,
        summary: str,
        input_digest: str | None = None,
        output_digest: str | None = None,
        latency_ms: int | None = None,
    ) -> AgentStep:
        """追加一条步骤行并立即提交。"""
        step = AgentStep(
            agent_run_id=self._agent_run.id,
            step_index=self._next_index,
            step_type=step_type,
            status=status,
            summary=redact_text(summary)[:STEP_SUMMARY_CHARS],
            input_digest=input_digest,
            output_digest=output_digest,
            latency_ms=latency_ms,
        )
        self._next_index += 1
        self._session.add(step)
        self._session.commit()
        return step

    def link_tool_calls(self, step_id: int, call_ids: Sequence[int]) -> None:
        """把某个节点的工具调用挂到该节点刚写下的那条步骤上。

        顺序由图固定：工具调用先执行并提交（每一条都是必须扛过崩溃的证据），之后
        才写 `agent_steps` 行，因此调用执行时步骤 id 还不存在。没有这一步，这些
        调用会一直留着空的 `agent_step_id`，运行页面就会报告每条步骤都没有
        工具调用。
        """
        if not call_ids:
            return
        self._session.execute(
            update(ToolCall).where(ToolCall.id.in_(list(call_ids))).values(agent_step_id=step_id)
        )
        self._session.commit()

    def sync_counters(
        self,
        *,
        loop_count: int,
        tool_call_count: int,
        model_call_count: int,
        prompt_tokens: int,
        completion_tokens: int,
        estimated_cost: Decimal,
    ) -> None:
        """把图内计数器镜像到运行行上，方便直接查询。"""
        self._agent_run.loop_count = loop_count
        self._agent_run.tool_call_count = tool_call_count
        self._agent_run.model_call_count = model_call_count
        self._agent_run.prompt_tokens = prompt_tokens
        self._agent_run.completion_tokens = completion_tokens
        self._agent_run.estimated_cost = estimated_cost
        self._session.commit()

    def finish(self, *, status: str, error_code: str | None = None) -> None:
        """写入运行的终态状态。"""
        self._agent_run.status = status
        self._agent_run.error_code = error_code
        self._agent_run.finished_at = utc_now()
        self._session.commit()
