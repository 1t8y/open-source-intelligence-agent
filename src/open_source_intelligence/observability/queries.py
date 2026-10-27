"""Read-side queries behind the observability overview.

`reports/queries.py` set the precedent this module follows: the API and web layers call these
functions and never issue SQL themselves, and a window predicate is part of the `WHERE` clause
rather than a post-filter applied afterwards.

Nothing in this module writes a row. The overview is a pure read of what the pipeline, the
Agent, the report and the evaluation layers already recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from open_source_intelligence.core.config import Settings
from open_source_intelligence.evaluation.errors import EvaluationError
from open_source_intelligence.evaluation.runner import compare_latest
from open_source_intelligence.persistence.models.agent import (
    AgentRun,
    AgentRunReview,
    AgentStep,
    ToolCall,
)
from open_source_intelligence.persistence.models.evaluation import EvalRun
from open_source_intelligence.persistence.models.intelligence import PipelineRun, Topic
from open_source_intelligence.persistence.models.reporting import Report

from .metrics import (
    AGENT_OUTCOME_STATUSES,
    AGENT_SUCCEEDED_STATUSES,
    MANUAL_BLOCK_STATUS,
    MAX_WINDOW_DAYS,
    MIN_WINDOW_DAYS,
    PIPELINE_OUTCOME_STATUSES,
    PIPELINE_SUCCEEDED_STATUSES,
    TOOL_SUCCEEDED_STATUSES,
    AgentMetrics,
    EvaluationMetrics,
    EvaluationTarget,
    MetricsOverview,
    PipelineMetrics,
    ReportMetrics,
    ToolBreakdown,
    ToolMetrics,
    Window,
    count_statuses,
    mean_decimal,
    quantize_rate,
    success_rate,
    summarize_latency,
)

# The documented list contract limits `page_size` to 1-100; the same bound is applied here so
# every list endpoint in the platform refuses an unbounded page the same way.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class AgentRunPage:
    """One page of Agent runs plus the total matching the filters."""

    items: tuple[AgentRun, ...]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class StepTrace:
    """One Agent step with the tool calls that hang off it."""

    step: AgentStep
    tool_calls: tuple[ToolCall, ...]


@dataclass(frozen=True, slots=True)
class AgentRunTrace:
    """A run's full trajectory, plus the report it produced when it produced one."""

    run: AgentRun
    topic_name: str | None
    report_id: int | None
    steps: tuple[StepTrace, ...]
    unlinked_tool_calls: tuple[ToolCall, ...]


def clamp_page_size(page_size: int) -> int:
    """Keep a requested page size inside the documented 1-100 range."""
    return max(1, min(page_size, MAX_PAGE_SIZE))


def resolve_window(*, now: datetime, window_days: int) -> Window:
    """Clamp a requested window and turn it into the span every metric is computed over."""
    days = max(MIN_WINDOW_DAYS, min(window_days, MAX_WINDOW_DAYS))
    return Window(days=days, start=now - timedelta(days=days), end=now)


def collect_metrics(
    session: Session,
    *,
    now: datetime,
    window_days: int,
    settings: Settings | None = None,
) -> MetricsOverview:
    """Aggregate every dashboard section for one window in a single read."""
    window = resolve_window(now=now, window_days=window_days)
    return MetricsOverview(
        window=window,
        pipeline=_pipeline_metrics(session, window),
        agent=_agent_metrics(session, window),
        tools=_tool_metrics(session, window),
        reports=_report_metrics(session, window),
        evaluations=_evaluation_metrics(session, settings),
    )


def list_agent_runs(
    session: Session,
    *,
    status: str | None = None,
    topic_id: int | None = None,
    review: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> AgentRunPage:
    """Return one page of Agent runs, newest first.

    `review` narrows by whether a human has already written a conclusion on the run: `"open"` for
    the ones nobody has read, `"done"` for the ones somebody has. It is a filter on this query
    rather than a post-filter on the page so `total` counts what the reader is actually looking
    at — a page of ten where eight are filtered out would otherwise still claim 50.
    """
    resolved_page = max(1, page)
    resolved_size = clamp_page_size(page_size)
    conditions = []
    if status is not None:
        conditions.append(AgentRun.status == status)
    if topic_id is not None:
        conditions.append(AgentRun.topic_id == topic_id)
    if review == "open":
        conditions.append(~exists().where(AgentRunReview.agent_run_id == AgentRun.id))
    elif review == "done":
        conditions.append(exists().where(AgentRunReview.agent_run_id == AgentRun.id))

    total = int(session.scalar(select(func.count()).select_from(AgentRun).where(*conditions)) or 0)
    items = session.scalars(
        select(AgentRun)
        .where(*conditions)
        .order_by(AgentRun.id.desc())
        .offset((resolved_page - 1) * resolved_size)
        .limit(resolved_size)
    ).all()
    return AgentRunPage(
        items=tuple(items),
        page=resolved_page,
        page_size=resolved_size,
        total=total,
    )


def get_agent_run(session: Session, *, run_id: int) -> AgentRun | None:
    """Return one Agent run, or `None` when it does not exist."""
    return session.get(AgentRun, run_id)


def get_agent_run_trace(session: Session, *, run_id: int) -> AgentRunTrace | None:
    """Return a run's ordered steps, the tool calls under each step, and its report.

    Tool calls whose `agent_step_id` is null are returned separately rather than dropped. The
    foreign key degrades to null when a step row is removed, and silently losing the call is
    exactly the evidence an operator needs when they are tracing a failure back to a tool.
    """
    run = session.get(AgentRun, run_id)
    if run is None:
        return None

    steps = session.scalars(
        select(AgentStep).where(AgentStep.agent_run_id == run.id).order_by(AgentStep.step_index)
    ).all()
    calls = session.scalars(
        select(ToolCall).where(ToolCall.agent_run_id == run.id).order_by(ToolCall.id)
    ).all()

    by_step: dict[int, list[ToolCall]] = {}
    unlinked: list[ToolCall] = []
    step_ids = {step.id for step in steps}
    for call in calls:
        if call.agent_step_id is not None and call.agent_step_id in step_ids:
            by_step.setdefault(call.agent_step_id, []).append(call)
        else:
            unlinked.append(call)

    topic_name = session.scalar(select(Topic.name).where(Topic.id == run.topic_id))
    report_id = session.scalar(select(Report.id).where(Report.agent_run_id == run.id))

    return AgentRunTrace(
        run=run,
        topic_name=topic_name,
        report_id=report_id,
        steps=tuple(
            StepTrace(step=step, tool_calls=tuple(by_step.get(step.id, ()))) for step in steps
        ),
        unlinked_tool_calls=tuple(unlinked),
    )


def _durations_ms(pairs: list[tuple[datetime | None, datetime | None]]) -> list[int]:
    """Turn (start, finish) pairs into whole milliseconds, skipping unfinished rows."""
    return [
        int((finished - started).total_seconds() * 1000)
        for started, finished in pairs
        if started is not None and finished is not None
    ]


def _pipeline_metrics(session: Session, window: Window) -> PipelineMetrics:
    """Aggregate scheduled pipeline runs.

    Rows are attributed to a window by `started_at`, so a run that never started belongs to no
    window. That is also why the run-type and status counts can be smaller than the number of
    rows an operator sees in the queue itself.
    """
    rows = session.scalars(
        select(PipelineRun).where(
            PipelineRun.started_at >= window.start,
            PipelineRun.started_at < window.end,
        )
    ).all()

    by_run_type: dict[str, dict[str, int]] = {}
    for row in rows:
        counts = by_run_type.setdefault(row.run_type, {})
        counts[row.status] = counts.get(row.status, 0) + 1

    outcome = [row for row in rows if row.status in PIPELINE_OUTCOME_STATUSES]
    succeeded = sum(1 for row in outcome if row.status in PIPELINE_SUCCEEDED_STATUSES)
    return PipelineMetrics(
        total=len(rows),
        by_status=count_statuses(row.status for row in rows),
        by_run_type={
            name: dict(sorted(counts.items())) for name, counts in sorted(by_run_type.items())
        },
        success_rate=success_rate(succeeded=succeeded, denominator=len(outcome)),
        latency=summarize_latency(
            _durations_ms([(row.started_at, row.finished_at) for row in rows])
        ),
    )


def _agent_metrics(session: Session, window: Window) -> AgentMetrics:
    """Aggregate Agent runs, their token spend, and why the failing ones failed."""
    rows = session.scalars(
        select(AgentRun).where(
            AgentRun.started_at >= window.start,
            AgentRun.started_at < window.end,
        )
    ).all()

    outcome = [row for row in rows if row.status in AGENT_OUTCOME_STATUSES]
    succeeded = sum(1 for row in outcome if row.status in AGENT_SUCCEEDED_STATUSES)
    prompt_tokens = sum(row.prompt_tokens for row in rows)
    completion_tokens = sum(row.completion_tokens for row in rows)
    total_tokens = prompt_tokens + completion_tokens
    error_codes: list[str] = [row.error_code for row in rows if row.error_code is not None]
    publish_modes: list[str] = [row.publish_mode for row in rows if row.publish_mode is not None]
    tokens_per_run: Decimal | None = None
    if rows:
        tokens_per_run = quantize_rate(Decimal(total_tokens) / Decimal(len(rows)))

    return AgentMetrics(
        total=len(rows),
        by_status=count_statuses(row.status for row in rows),
        by_publish_mode=count_statuses(publish_modes),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        tokens_per_run=tokens_per_run,
        estimated_cost=sum((row.estimated_cost for row in rows), Decimal("0")),
        success_rate=success_rate(succeeded=succeeded, denominator=len(outcome)),
        failure_reasons=count_statuses(error_codes),
        latency=summarize_latency(
            _durations_ms([(row.started_at, row.finished_at) for row in rows])
        ),
    )


def _tool_metrics(session: Session, window: Window) -> ToolMetrics:
    """Aggregate whitelisted tool usage, busiest tool first."""
    rows = session.scalars(
        select(ToolCall).where(
            ToolCall.created_at >= window.start,
            ToolCall.created_at < window.end,
        )
    ).all()

    grouped: dict[str, list[ToolCall]] = {}
    for row in rows:
        grouped.setdefault(row.tool_name, []).append(row)

    breakdown: list[ToolBreakdown] = []
    for tool_name, calls in grouped.items():
        succeeded = sum(1 for call in calls if call.status in TOOL_SUCCEEDED_STATUSES)
        breakdown.append(
            ToolBreakdown(
                tool_name=tool_name,
                total=len(calls),
                by_status=count_statuses(call.status for call in calls),
                success_rate=success_rate(succeeded=succeeded, denominator=len(calls)),
                latency=summarize_latency(
                    [call.latency_ms for call in calls if call.latency_ms is not None]
                ),
            )
        )
    breakdown.sort(key=lambda item: (-item.total, item.tool_name))

    succeeded_total = sum(1 for row in rows if row.status in TOOL_SUCCEEDED_STATUSES)
    return ToolMetrics(
        total=len(rows),
        by_status=count_statuses(row.status for row in rows),
        success_rate=success_rate(succeeded=succeeded_total, denominator=len(rows)),
        by_tool=tuple(breakdown),
    )


def _report_metrics(session: Session, window: Window) -> ReportMetrics:
    """Aggregate report production and ARCHITECTURE §12's manual block rate."""
    rows = session.scalars(
        select(Report).where(
            Report.generated_at >= window.start,
            Report.generated_at < window.end,
        )
    ).all()

    by_status = count_statuses(row.status for row in rows)
    block_rate: Decimal | None = None
    if rows:
        blocked = by_status.get(MANUAL_BLOCK_STATUS, 0)
        block_rate = quantize_rate(Decimal(blocked) / Decimal(len(rows)))
    return ReportMetrics(
        total=len(rows),
        by_status=by_status,
        manual_block_rate=block_rate,
    )


def _evaluation_metrics(session: Session, settings: Settings | None) -> EvaluationMetrics:
    """Surface the newest evaluation sweep without re-running or re-judging anything.

    The verdict is read from `compare_latest`, not recomputed here, so a green dashboard and a
    green evaluation report are the same computation rather than two implementations that
    happen to agree today.
    """
    latest_run = session.scalars(select(EvalRun).order_by(EvalRun.id.desc()).limit(1)).first()
    if latest_run is None:
        return EvaluationMetrics(
            dataset_id=None,
            dataset_name=None,
            dataset_version=None,
            passed=None,
            regressions=(),
            targets=(),
        )
    try:
        report = compare_latest(session, dataset_id=latest_run.dataset_id, settings=settings)
    except EvaluationError:
        return EvaluationMetrics(
            dataset_id=None,
            dataset_name=None,
            dataset_version=None,
            passed=None,
            regressions=(),
            targets=(),
        )
    return EvaluationMetrics(
        dataset_id=report.dataset_id,
        dataset_name=report.dataset_name,
        dataset_version=report.dataset_version,
        passed=report.passed,
        regressions=tuple(item.target_type for item in report.targets if item.regression),
        targets=tuple(EvaluationTarget.from_summary(item) for item in report.targets),
    )


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "AgentRunPage",
    "AgentRunTrace",
    "StepTrace",
    "clamp_page_size",
    "collect_metrics",
    "get_agent_run",
    "get_agent_run_trace",
    "list_agent_runs",
    "mean_decimal",
    "resolve_window",
]
