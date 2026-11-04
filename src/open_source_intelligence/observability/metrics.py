"""对运行台账做确定性的指标聚合。

这里的一切都由平台已经写下的行算出来，不从模型推断任何东西。有两条约定是有意从评测层照搬
过来的，这样仪表盘与评测报告永远不会把同一个数字舍入或留空成不同的样子：

* 比率是 `Decimal`，用 `ROUND_HALF_UP` 量化到四位小数；
* 从未被行使过的指标是 `None`，绝不是 `0`。

第二条规则是承重的那一条。空窗口必须与一个满是真实零值的窗口保持可区分，否则「还没跑过
任何东西」就会被读成「一切都在失败」。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from open_source_intelligence.evaluation.runner import TargetSummary

RATE_PLACES: Final[Decimal] = Decimal("0.0001")
DEFAULT_WINDOW_DAYS: Final[int] = 7
#: 窗口的上下界。端点会拒绝越界的值而不是把它夹到边界内，处理方式与拒绝越界的 `page_size`
#: 相同；`resolve_window` 本身仍会夹取，因此直接调用者无法把比台账预期范围更宽的窗口交给
#: 聚合。
MIN_WINDOW_DAYS: Final[int] = 1
MAX_WINDOW_DAYS: Final[int] = 90

#: 处于这些状态之一的 pipeline 行才算有了结果。`queued` 与 `running` 没有结果，因此它们计入
#: `total`，却被排除在成功率的算之外。
PIPELINE_OUTCOME_STATUSES: Final[tuple[str, ...]] = ("succeeded", "failed", "partial")
PIPELINE_SUCCEEDED_STATUSES: Final[tuple[str, ...]] = ("succeeded",)
#: `needs_review` 产出了报告，只是把它留在那里等人看，所以运行本身是成功的；`failed` 是
#: 唯一算输的结果。
AGENT_SUCCEEDED_STATUSES: Final[tuple[str, ...]] = ("completed", "needs_review")
AGENT_OUTCOME_STATUSES: Final[tuple[str, ...]] = ("completed", "needs_review", "failed")
#: 被拒绝的调用是一次被驳回的越权尝试，从计量角度看就是一次失败调用 —— 把它排除在外，会让
#: 一个试图越出自身工具边界的 Agent 看起来更健康。
TOOL_FAILED_STATUSES: Final[tuple[str, ...]] = ("failed", "denied")
TOOL_SUCCEEDED_STATUSES: Final[tuple[str, ...]] = ("success",)
#: ARCHITECTURE §12 中「常规报告人工阻塞率」所指的报告状态。
MANUAL_BLOCK_STATUS: Final[str] = "pending_review"


def quantize_rate(value: Decimal) -> Decimal:
    """按评测层的方式舍入一个比率。"""
    return value.quantize(RATE_PLACES, rounding=ROUND_HALF_UP)


def as_rate(value: Decimal | None) -> str | None:
    """把比率渲染成 JSON 形式，不为「未测量」凭空编一个数字。"""
    return None if value is None else str(value)


def success_rate(*, succeeded: int, denominator: int) -> Decimal | None:
    """返回 `succeeded / denominator`，没有任何东西达到结果时返回 `None`。"""
    if denominator <= 0:
        return None
    return quantize_rate(Decimal(succeeded) / Decimal(denominator))


def mean_decimal(values: Sequence[Decimal]) -> Decimal | None:
    """返回量化后的均值，样本为空时返回 `None`。"""
    if not values:
        return None
    return quantize_rate(sum(values) / Decimal(len(values)))


def percentile(values: Sequence[int], percent: int) -> int | None:
    """返回最近秩百分位数，样本为空时返回 `None`。

    这里用最近秩而不是插值定义，因为它返回的永远是一个某次运行真实花掉的时长。运维看
    「P95 延迟」要的是一个真实、偏最差的情况，而不是两次运行之间的合成中点。
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percent / 100 * len(ordered)))
    return ordered[rank - 1]


def count_statuses(statuses: Iterable[str]) -> dict[str, int]:
    """按状态统计行数；没出现过的状态是缺席，而不是零。"""
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def summarize_latency(values: Sequence[int]) -> Latency:
    """用一份以毫秒计的时长样本构造百分位摘要。"""
    return Latency(
        samples=len(values),
        p50_ms=percentile(values, 50),
        p95_ms=percentile(values, 95),
    )


@dataclass(frozen=True, slots=True)
class Window:
    """一次响应里所有指标所依据的半开时间区间。"""

    days: int
    start: datetime
    end: datetime

    def to_dict(self) -> dict[str, Any]:
        """返回可安全序列化为 JSON 的载荷。"""
        return {"days": self.days, "start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True, slots=True)
class Latency:
    """一份时长样本的百分位数，以及支撑它们的观测条数。"""

    samples: int
    p50_ms: int | None
    p95_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        """返回可安全序列化为 JSON 的载荷。"""
        return {"samples": self.samples, "p50_ms": self.p50_ms, "p95_ms": self.p95_ms}


@dataclass(frozen=True, slots=True)
class PipelineMetrics:
    """定时 pipeline 任务的吞吐量与健康状况。"""

    total: int
    by_status: dict[str, int]
    by_run_type: dict[str, dict[str, int]]
    success_rate: Decimal | None
    latency: Latency

    def to_dict(self) -> dict[str, Any]:
        """返回可安全序列化为 JSON 的载荷。"""
        return {
            "total": self.total,
            "by_status": self.by_status,
            "by_run_type": self.by_run_type,
            "success_rate": as_rate(self.success_rate),
            "latency": self.latency.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class AgentMetrics:
    """研究 Agent 的预算消耗、结果与失败原因。"""

    total: int
    by_status: dict[str, int]
    by_publish_mode: dict[str, int]
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    tokens_per_run: Decimal | None
    estimated_cost: Decimal
    success_rate: Decimal | None
    failure_reasons: dict[str, int]
    latency: Latency

    def to_dict(self) -> dict[str, Any]:
        """返回可安全序列化为 JSON 的载荷。"""
        return {
            "total": self.total,
            "by_status": self.by_status,
            "by_publish_mode": self.by_publish_mode,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "tokens_per_run": as_rate(self.tokens_per_run),
            "estimated_cost": str(self.estimated_cost),
            "success_rate": as_rate(self.success_rate),
            "failure_reasons": self.failure_reasons,
            "latency": self.latency.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ToolBreakdown:
    """单个工具的调用计数与延迟，让某个表现异常的工具能被单独看见。"""

    tool_name: str
    total: int
    by_status: dict[str, int]
    success_rate: Decimal | None
    latency: Latency

    def to_dict(self) -> dict[str, Any]:
        """返回可安全序列化为 JSON 的载荷。"""
        return {
            "tool_name": self.tool_name,
            "total": self.total,
            "by_status": self.by_status,
            "success_rate": as_rate(self.success_rate),
            "latency": self.latency.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ToolMetrics:
    """白名单工具的使用情况，按最繁忙者排在最前。"""

    total: int
    by_status: dict[str, int]
    success_rate: Decimal | None
    by_tool: tuple[ToolBreakdown, ...]

    def to_dict(self) -> dict[str, Any]:
        """返回可安全序列化为 JSON 的载荷。"""
        return {
            "total": self.total,
            "by_status": self.by_status,
            "success_rate": as_rate(self.success_rate),
            "by_tool": [item.to_dict() for item in self.by_tool],
        }


@dataclass(frozen=True, slots=True)
class ReportMetrics:
    """报告产出情况，以及其中有多少在发出去之前需要人工把关。"""

    total: int
    by_status: dict[str, int]
    manual_block_rate: Decimal | None

    def to_dict(self) -> dict[str, Any]:
        """返回可安全序列化为 JSON 的载荷。"""
        return {
            "total": self.total,
            "by_status": self.by_status,
            "manual_block_rate": as_rate(self.manual_block_rate),
        }


@dataclass(frozen=True, slots=True)
class EvaluationTarget:
    """最近一次评测批量中的一个变体，从评测层搬运过来。"""

    target_type: str
    status: str
    scored_case_count: int
    metrics: dict[str, str | None]
    total_tokens: int
    mean_latency_ms: int
    reference_target: str | None
    gate_failures: tuple[str, ...]
    regression: bool

    @classmethod
    def from_summary(cls, summary: TargetSummary) -> EvaluationTarget:
        """用评测层自己的摘要对象构造一行仪表盘数据。"""
        return cls(
            target_type=summary.target_type,
            status=summary.status,
            scored_case_count=summary.scored_case_count,
            metrics={name: as_rate(value) for name, value in summary.metrics.items()},
            total_tokens=summary.total_tokens,
            mean_latency_ms=summary.mean_latency_ms,
            reference_target=summary.reference_target,
            gate_failures=tuple(summary.gate_failures),
            regression=summary.regression,
        )

    def to_dict(self) -> dict[str, Any]:
        """返回可安全序列化为 JSON 的载荷。"""
        return {
            "target_type": self.target_type,
            "status": self.status,
            "scored_case_count": self.scored_case_count,
            "metrics": self.metrics,
            "total_tokens": self.total_tokens,
            "mean_latency_ms": self.mean_latency_ms,
            "reference_target": self.reference_target,
            "gate_failures": list(self.gate_failures),
            "regression": self.regression,
        }


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    """最近一次评测批量，原样复用而不重新计算。

    仪表盘从不自己重推 `passed`：它取自 `evaluation.runner.compare_latest`。因此一个全绿的
    仪表盘不可能与它所概括的评测报告不一致，两者也永远不会随着判定规则改变而漂开。

    与其它几节不同，这一节刻意**不**按窗口切分。评测由运维触发、频次很低，1 天的窗口通常
    什么都看不到；最新那一批才是值得呈现的东西。
    """

    dataset_id: int | None
    dataset_name: str | None
    dataset_version: str | None
    passed: bool | None
    regressions: tuple[str, ...]
    targets: tuple[EvaluationTarget, ...]

    @property
    def available(self) -> bool:
        """返回历史上是否记录过任何一次评测批量。"""
        return self.dataset_id is not None

    def to_dict(self) -> dict[str, Any]:
        """返回可安全序列化为 JSON 的载荷。"""
        dataset: dict[str, Any] | None = None
        if self.dataset_id is not None:
            dataset = {
                "id": self.dataset_id,
                "name": self.dataset_name,
                "version": self.dataset_version,
            }
        return {
            "dataset": dataset,
            "passed": self.passed,
            "regressions": list(self.regressions),
            "targets": [item.to_dict() for item in self.targets],
        }


@dataclass(frozen=True, slots=True)
class MetricsOverview:
    """整个仪表盘的载荷：一个窗口、五个小节、零写入。"""

    window: Window
    pipeline: PipelineMetrics
    agent: AgentMetrics
    tools: ToolMetrics
    reports: ReportMetrics
    evaluations: EvaluationMetrics

    def to_dict(self) -> dict[str, Any]:
        """返回可安全序列化为 JSON 的载荷。"""
        return {
            "window": self.window.to_dict(),
            "pipeline": self.pipeline.to_dict(),
            "agent": self.agent.to_dict(),
            "tools": self.tools.to_dict(),
            "reports": self.reports.to_dict(),
            "evaluations": self.evaluations.to_dict(),
        }


__all__ = [
    "AGENT_OUTCOME_STATUSES",
    "AGENT_SUCCEEDED_STATUSES",
    "DEFAULT_WINDOW_DAYS",
    "MANUAL_BLOCK_STATUS",
    "MAX_WINDOW_DAYS",
    "MIN_WINDOW_DAYS",
    "PIPELINE_OUTCOME_STATUSES",
    "PIPELINE_SUCCEEDED_STATUSES",
    "RATE_PLACES",
    "TOOL_FAILED_STATUSES",
    "TOOL_SUCCEEDED_STATUSES",
    "AgentMetrics",
    "EvaluationMetrics",
    "EvaluationTarget",
    "Latency",
    "MetricsOverview",
    "PipelineMetrics",
    "ReportMetrics",
    "ToolBreakdown",
    "ToolMetrics",
    "Window",
    "as_rate",
    "count_statuses",
    "mean_decimal",
    "percentile",
    "quantize_rate",
    "success_rate",
    "summarize_latency",
]
