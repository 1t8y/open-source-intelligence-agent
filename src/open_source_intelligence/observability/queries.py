"""提供可观测性总览背后的读侧查询。

本模块沿用 `reports/queries.py` 立下的先例：API 与 Web 层调用这些函数，自己从不发 SQL；
窗口条件写进 `WHERE` 子句里，而不是事后再做一次过滤。

本模块不写任何一行。总览是对 pipeline、Agent、报告与评测各层已经记录的内容的纯读取。
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

# 文档化的列表契约把 `page_size` 限制在 1-100；这里施加同一个界，让平台上每个列表端点都以
# 同一种方式拒绝无界的分页。
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class AgentRunPage:
    """一页 Agent 运行，加上符合筛选条件的总数。"""

    items: tuple[AgentRun, ...]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class StepTrace:
    """一个 Agent 步骤，以及挂在它下面的工具调用。"""

    step: AgentStep
    tool_calls: tuple[ToolCall, ...]


@dataclass(frozen=True, slots=True)
class AgentRunTrace:
    """一次运行的完整轨迹，以及它在产出过报告时的那份报告。"""

    run: AgentRun
    topic_name: str | None
    report_id: int | None
    steps: tuple[StepTrace, ...]
    unlinked_tool_calls: tuple[ToolCall, ...]


def clamp_page_size(page_size: int) -> int:
    """把请求的分页大小限制在文档规定的 1-100 区间内。"""
    return max(1, min(page_size, MAX_PAGE_SIZE))


def resolve_window(*, now: datetime, window_days: int) -> Window:
    """夹取请求的窗口，并把它变成所有指标所依据的时间跨度。"""
    days = max(MIN_WINDOW_DAYS, min(window_days, MAX_WINDOW_DAYS))
    return Window(days=days, start=now - timedelta(days=days), end=now)


def collect_metrics(
    session: Session,
    *,
    now: datetime,
    window_days: int,
    settings: Settings | None = None,
) -> MetricsOverview:
    """在一次读取里聚合某个窗口下的每个仪表盘小节。"""
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
    """返回一页 Agent 运行，最新的在前。

    `review` 按「是否已有人写下结论」收窄范围：`"open"` 指还没人读过的，`"done"` 指已经有人
    读过的。它是这条查询上的筛选条件，而不是对已取回的这一页做后置过滤，因此 `total` 统计的
    就是读者真正看到的东西 —— 否则一页十条里有八条被过滤掉，它仍然会声称有 50 条。
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
    """返回一次 Agent 运行，不存在时返回 `None`。"""
    return session.get(AgentRun, run_id)


def get_agent_run_trace(session: Session, *, run_id: int) -> AgentRunTrace | None:
    """返回一次运行按序排列的步骤、每个步骤下的工具调用，以及它的报告。

    `agent_step_id` 为 null 的工具调用会单独返回，而不是被丢掉。步骤行被删除时外键会退化成
    null，而一次调用被悄悄吞掉，恰恰是运维把失败追溯回某个工具时最需要的那份证据。
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
    """把（开始，结束）时间对转成整毫秒，跳过尚未完成的行。"""
    return [
        int((finished - started).total_seconds() * 1000)
        for started, finished in pairs
        if started is not None and finished is not None
    ]


def _pipeline_metrics(session: Session, window: Window) -> PipelineMetrics:
    """聚合定时的 pipeline 运行。

    行按 `started_at` 归属到窗口，因此从未开始的运行不属于任何窗口。这也是为什么按运行类型
    和状态的计数，可能小于运维在队列本身里看到的行数。
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
    """聚合 Agent 运行、它们的 token 开销，以及失败的那些为什么失败。"""
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
    """聚合白名单工具的使用情况，最繁忙的工具在前。"""
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
    """聚合报告产出情况以及 ARCHITECTURE §12 的人工阻塞率。"""
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
    """呈现最新一次评测批量，不重跑、也不重判任何东西。

    结论读自 `compare_latest`，不在这里重算，因此全绿的仪表盘与全绿的评测报告是同一次计算，
    而不是两套恰好今天还一致的实现。
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
