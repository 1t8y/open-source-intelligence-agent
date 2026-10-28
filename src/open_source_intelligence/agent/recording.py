"""Persist the Agent run trail: one ordered step per node plus live counters.

Every step is committed immediately, so a crash mid-run still leaves the evidence that
the run got that far. Summaries are redacted before they are stored (INV-8).
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

#: Step summaries are diagnostics, not payload storage, so they are capped.
STEP_SUMMARY_CHARS = 1000


def digest_text(value: str) -> str:
    """Return a stable SHA-256 digest for one step input or output."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class StepRecorder:
    """Append ordered `agent_steps` rows and keep the owning run row in sync."""

    def __init__(self, session: Session, agent_run: AgentRun) -> None:
        self._session = session
        self._agent_run = agent_run
        self._next_index = self._current_max_index() + 1

    def _current_max_index(self) -> int:
        """Return the highest step index already stored for this run."""
        current = self._session.scalar(
            select(func.max(AgentStep.step_index)).where(
                AgentStep.agent_run_id == self._agent_run.id
            )
        )
        return int(current) if current is not None else 0

    def mark_running(self) -> None:
        """Flag the run as in progress with a start timestamp."""
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
        """Append one step row and commit it immediately."""
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
        """Attach the tool calls of one node to the step that node just wrote.

        The order is fixed by the graph: tool calls are executed and committed first (each one is
        evidence that must survive a crash), and only then is the `agent_steps` row written, so the
        step id does not exist while the calls run. Without this the calls kept a null
        `agent_step_id` forever and the run page reported every step as having no tool calls.
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
        """Mirror the in-graph counters onto the run row for queryability."""
        self._agent_run.loop_count = loop_count
        self._agent_run.tool_call_count = tool_call_count
        self._agent_run.model_call_count = model_call_count
        self._agent_run.prompt_tokens = prompt_tokens
        self._agent_run.completion_tokens = completion_tokens
        self._agent_run.estimated_cost = estimated_cost
        self._session.commit()

    def finish(self, *, status: str, error_code: str | None = None) -> None:
        """Write the terminal status of the run."""
        self._agent_run.status = status
        self._agent_run.error_code = error_code
        self._agent_run.finished_at = utc_now()
        self._session.commit()
