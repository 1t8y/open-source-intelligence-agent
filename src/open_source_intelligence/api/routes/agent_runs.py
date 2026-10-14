"""Agent run ledger: the run list, one run, and a run's trajectory.

The trace hangs off `/{run_id}/steps` rather than being folded into `/{run_id}` so an operator
can page through runs cheaply and only pull the step-and-tool detail for the run they are
actually investigating. `/runs` is declared before `/{run_id}` for the same reason the report
routes declare their literal paths first.
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
    """One research run, with its budget consumption and outcome."""

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
    """One page of runs, newest first."""

    items: list[AgentRunResponse]
    page: int
    page_size: int
    total: int


class ToolCallResponse(BaseModel):
    """One recorded tool invocation, with credential-shaped text already redacted."""

    id: int
    tool_name: str
    status: str
    error_code: str | None
    latency_ms: int | None
    result_summary: str | None
    arguments: dict[str, Any]
    created_at: str


class AgentStepResponse(BaseModel):
    """One ordered step and the tool calls hanging off it."""

    id: int
    step_index: int
    step_type: str
    status: str
    summary: str | None
    latency_ms: int | None
    created_at: str
    tool_calls: list[ToolCallResponse]


class AgentRunTraceResponse(BaseModel):
    """A run's trajectory: its steps, its tool calls, and the report it produced."""

    run: AgentRunResponse
    topic_name: str | None
    report_id: int | None
    steps: list[AgentStepResponse]
    unlinked_tool_calls: list[ToolCallResponse]


def _latency_ms(run: AgentRun) -> int | None:
    """Return the wall-clock duration of a finished run, or `None` while it is open."""
    if run.started_at is None or run.finished_at is None:
        return None
    return int((run.finished_at - run.started_at).total_seconds() * 1000)


def _run_response(run: AgentRun) -> AgentRunResponse:
    """Serialize one run row."""
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
    """Redact credential-shaped substrings before tool arguments leave the process.

    Tool arguments are written by the tool layer and are not free-form text, but the values can
    still echo user input. Passing them through the same redaction the logs use keeps one rule
    for "what may not be exported" instead of two.
    """
    redacted = redact_text(json.dumps(arguments, ensure_ascii=False, default=str))
    decoded: Any = json.loads(redacted)
    return decoded if isinstance(decoded, dict) else {}


def _tool_call_response(call: ToolCall) -> ToolCallResponse:
    """Serialize one tool call, redacting its free text."""
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
    """Serialize a whole trajectory in step order."""
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
    """Return one page of Agent runs, newest first, optionally filtered."""
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
    """Return one Agent run's status, budget and outcome."""
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
    """Return a run's ordered steps and tool calls, so a failure reaches its tool call.

    This is the endpoint the "追到具体工具调用" acceptance is checked against: given a failed
    run, the response carries the step that failed, the tool that was called, its status, and
    its error code.
    """
    trace = get_agent_run_trace(session, run_id=run_id)
    if trace is None:
        raise AgentRunNotFound(f"agent run {run_id} was not found")
    return _trace_response(trace)
