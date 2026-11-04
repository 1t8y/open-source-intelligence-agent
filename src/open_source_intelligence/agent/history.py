"""对 Agent 运行轨迹的只读查询。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.agent import AgentRun, AgentStep, ToolCall

DEFAULT_RUN_LIMIT = 20
RUN_LIMIT_MAX = 200


def _iso(value: datetime | None) -> str | None:
    """把可选时间戳渲染成 ISO 8601。"""
    return None if value is None else value.isoformat()


def get_agent_run(session: Session, run_id: int) -> AgentRun | None:
    """按 id 返回一条 Agent 运行。"""
    return session.get(AgentRun, run_id)


def list_agent_runs(
    session: Session,
    *,
    topic_id: int | None = None,
    status: str | None = None,
    limit: int = DEFAULT_RUN_LIMIT,
) -> list[AgentRun]:
    """返回符合给定筛选条件的最新 Agent 运行。"""
    statement = select(AgentRun)
    if topic_id is not None:
        statement = statement.where(AgentRun.topic_id == topic_id)
    if status is not None:
        statement = statement.where(AgentRun.status == status)
    statement = statement.order_by(AgentRun.id.desc()).limit(limit)
    return list(session.scalars(statement))


def list_agent_steps(session: Session, run_id: int) -> list[AgentStep]:
    """按执行顺序返回一次运行的所有步骤。"""
    return list(
        session.scalars(
            select(AgentStep)
            .where(AgentStep.agent_run_id == run_id)
            .order_by(AgentStep.step_index.asc())
        )
    )


def list_tool_calls(session: Session, run_id: int) -> list[ToolCall]:
    """按调用顺序返回一次运行记录在案的所有工具调用。"""
    return list(
        session.scalars(
            select(ToolCall).where(ToolCall.agent_run_id == run_id).order_by(ToolCall.id.asc())
        )
    )


def summarize_agent_run(run: AgentRun) -> dict[str, Any]:
    """返回一次 Agent 运行的 JSON 安全摘要。"""
    return {
        "id": run.id,
        "pipeline_run_id": run.pipeline_run_id,
        "topic_id": run.topic_id,
        "agent_config_id": run.agent_config_id,
        "status": run.status,
        "window_start": _iso(run.window_start),
        "window_end": _iso(run.window_end),
        "loop_count": run.loop_count,
        "tool_call_count": run.tool_call_count,
        "model_call_count": run.model_call_count,
        "estimated_cost": str(run.estimated_cost),
        "publish_mode": run.publish_mode,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "error_code": run.error_code,
    }


def summarize_agent_steps(steps: list[AgentStep]) -> list[dict[str, Any]]:
    """返回一次运行中各步骤的 JSON 安全摘要。"""
    return [
        {
            "step_index": step.step_index,
            "step_type": step.step_type,
            "status": step.status,
            "summary": step.summary,
            "latency_ms": step.latency_ms,
        }
        for step in steps
    ]


def summarize_tool_calls(calls: list[ToolCall]) -> list[dict[str, Any]]:
    """返回一次运行中各工具调用的 JSON 安全摘要。"""
    return [
        {
            "id": call.id,
            "tool_name": call.tool_name,
            "status": call.status,
            "error_code": call.error_code,
            "latency_ms": call.latency_ms,
            "result_summary": call.result_summary,
        }
        for call in calls
    ]
