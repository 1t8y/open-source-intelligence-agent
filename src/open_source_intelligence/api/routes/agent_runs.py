"""Agent 运行台账：运行列表、单次运行，以及一次运行的轨迹。

轨迹挂在 `/{run_id}/steps` 而不是折进 `/{run_id}`，这样运维可以低成本地
翻页浏览运行，只对真正要排查的那一次再拉取步骤与工具调用的细节。`/runs`
声明在 `/{run_id}` 之前，理由和报告路由先声明字面量路径一样。
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from open_source_intelligence.core.logging import redact_text
from open_source_intelligence.observability.errors import AgentRunNotFound
from open_source_intelligence.observability.queries import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    AgentRunTrace,
    get_agent_run,
    get_agent_run_trace,
    list_agent_runs,
)
from open_source_intelligence.persistence.models.agent import AgentRun, ToolCall
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.security.authorization import METRICS_ROLES
from open_source_intelligence.security.dependencies import get_db_session, require_roles

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])

MetricsUser = Annotated[User, Depends(require_roles(*METRICS_ROLES))]
DbSession = Annotated[Session, Depends(get_db_session)]


class AgentRunResponse(BaseModel):
    """一次研究运行，连同它的预算消耗与结果。"""

    id: int
    pipeline_run_id: int
    topic_id: int
    agent_config_id: int
    status: str
    window_start: str
    window_end: str
    loop_count: int
    tool_call_count: int
    model_call_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: str
    publish_mode: str | None
    error_code: str | None
    started_at: str | None
    finished_at: str | None
    latency_ms: int | None


class AgentRunListResponse(BaseModel):
    """一页运行记录，最新的在前。"""

    items: list[AgentRunResponse]
    page: int
    page_size: int
    total: int


class ToolCallResponse(BaseModel):
    """一次工具调用记录，其中的凭据形态文本已经过脱敏。"""

    id: int
    tool_name: str
    status: str
    error_code: str | None
    latency_ms: int | None
    result_summary: str | None
    arguments: dict[str, Any]
    created_at: str


class AgentStepResponse(BaseModel):
    """一个有序步骤，以及挂在这个步骤下的工具调用。"""

    id: int
    step_index: int
    step_type: str
    status: str
    summary: str | None
    latency_ms: int | None
    created_at: str
    tool_calls: list[ToolCallResponse]


class AgentRunTraceResponse(BaseModel):
    """一次运行的轨迹：它的步骤、工具调用，以及它产出的报告。"""

    run: AgentRunResponse
    topic_name: str | None
    report_id: int | None
    steps: list[AgentStepResponse]
    unlinked_tool_calls: list[ToolCallResponse]


def _latency_ms(run: AgentRun) -> int | None:
    """返回一次已完成运行的墙钟耗时；运行尚未结束时返回 `None`。"""
    if run.started_at is None or run.finished_at is None:
        return None
    return int((run.finished_at - run.started_at).total_seconds() * 1000)


def _run_response(run: AgentRun) -> AgentRunResponse:
    """把一行运行记录序列化成响应。"""
    return AgentRunResponse(
        id=run.id,
        pipeline_run_id=run.pipeline_run_id,
        topic_id=run.topic_id,
        agent_config_id=run.agent_config_id,
        status=run.status,
        window_start=run.window_start.isoformat(),
        window_end=run.window_end.isoformat(),
        loop_count=run.loop_count,
        tool_call_count=run.tool_call_count,
        model_call_count=run.model_call_count,
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
        total_tokens=run.prompt_tokens + run.completion_tokens,
        estimated_cost=str(run.estimated_cost),
        publish_mode=run.publish_mode,
        error_code=run.error_code,
        started_at=None if run.started_at is None else run.started_at.isoformat(),
        finished_at=None if run.finished_at is None else run.finished_at.isoformat(),
        latency_ms=_latency_ms(run),
    )


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """在工具参数离开进程之前脱掉凭据形态的子串。

    工具参数由工具层写入，并不是自由文本，但其中的值仍可能回显用户输入。让它们走与日志同一套
    脱敏，是为了让「哪些内容不允许外带」只有一条规则，而不是两条。
    """
    redacted = redact_text(json.dumps(arguments, ensure_ascii=False, default=str))
    decoded: Any = json.loads(redacted)
    return decoded if isinstance(decoded, dict) else {}


def _tool_call_response(call: ToolCall) -> ToolCallResponse:
    """序列化一次工具调用，并脱敏它的自由文本。"""
    return ToolCallResponse(
        id=call.id,
        tool_name=call.tool_name,
        status=call.status,
        error_code=call.error_code,
        latency_ms=call.latency_ms,
        result_summary=None if call.result_summary is None else redact_text(call.result_summary),
        arguments=_safe_arguments(call.arguments_json),
        created_at=call.created_at.isoformat(),
    )


def _trace_response(trace: AgentRunTrace) -> AgentRunTraceResponse:
    """按步骤顺序序列化整条轨迹。"""
    return AgentRunTraceResponse(
        run=_run_response(trace.run),
        topic_name=trace.topic_name,
        report_id=trace.report_id,
        steps=[
            AgentStepResponse(
                id=item.step.id,
                step_index=item.step.step_index,
                step_type=item.step.step_type,
                status=item.step.status,
                summary=None if item.step.summary is None else redact_text(item.step.summary),
                latency_ms=item.step.latency_ms,
                created_at=item.step.created_at.isoformat(),
                tool_calls=[_tool_call_response(call) for call in item.tool_calls],
            )
            for item in trace.steps
        ],
        unlinked_tool_calls=[_tool_call_response(call) for call in trace.unlinked_tool_calls],
    )


@router.get("/runs", response_model=AgentRunListResponse)
def get_agent_runs(
    user: MetricsUser,
    session: DbSession,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    topic_id: Annotated[int | None, Query(ge=1)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> AgentRunListResponse:
    """返回一页 Agent 运行记录，最新的在前，可按条件筛选。"""
    result = list_agent_runs(
        session,
        status=status_filter,
        topic_id=topic_id,
        page=page,
        page_size=page_size,
    )
    return AgentRunListResponse(
        items=[_run_response(run) for run in result.items],
        page=result.page,
        page_size=result.page_size,
        total=result.total,
    )


@router.get("/runs/{run_id}", response_model=AgentRunResponse)
def get_agent_run_detail(
    user: MetricsUser,
    session: DbSession,
    run_id: Annotated[int, Path(ge=1)],
) -> AgentRunResponse:
    """返回一次 Agent 运行的状态、预算与结果。"""
    run = get_agent_run(session, run_id=run_id)
    if run is None:
        raise AgentRunNotFound(f"agent run {run_id} was not found")
    return _run_response(run)


@router.get("/runs/{run_id}/steps", response_model=AgentRunTraceResponse)
def get_agent_run_steps(
    user: MetricsUser,
    session: DbSession,
    run_id: Annotated[int, Path(ge=1)],
) -> AgentRunTraceResponse:
    """返回一次运行的有序步骤与工具调用，让失败能追到具体的那次调用。

    「追到具体工具调用」这条验收就是对着这个端点检查的：给定一次失败的运行，响应里带着失败的
    步骤、当时调用的工具、它的状态与错误码。
    """
    trace = get_agent_run_trace(session, run_id=run_id)
    if trace is None:
        raise AgentRunNotFound(f"agent run {run_id} was not found")
    return _trace_response(trace)
