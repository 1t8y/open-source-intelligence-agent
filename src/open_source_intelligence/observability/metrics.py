"""Deterministic metric aggregation over the run ledger.

Everything here is computed from rows the platform already wrote; nothing is inferred from a
model. Two conventions are copied from the evaluation layer on purpose, so the dashboard and
the evaluation report can never round or blank the same number differently:

* ratios are `Decimal` quantized to four places with `ROUND_HALF_UP`;
* a metric that was never exercised is `None`, never `0`.

The second rule is the load-bearing one. An empty window has to stay distinguishable from a
window full of real zeros, otherwise "nothing has run yet" reads as "everything is failing".
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
#: The window bounds. The endpoints refuse a value outside them rather than clamping, the same
#: way they refuse an out-of-range `page_size`; `resolve_window` clamps anyway, so a direct
#: caller cannot hand the aggregation a window wider than the ledger is meant to be read over.
MIN_WINDOW_DAYS: Final[int] = 1
MAX_WINDOW_DAYS: Final[int] = 90

#: A pipeline row in one of these states has an outcome. `queued` and `running` do not, so they
#: are counted in `total` but kept out of the success-rate denominator.
PIPELINE_OUTCOME_STATUSES: Final[tuple[str, ...]] = ("succeeded", "failed", "partial")
PIPELINE_SUCCEEDED_STATUSES: Final[tuple[str, ...]] = ("succeeded",)
#: `needs_review` produced a report and merely parked it for a human, so the run itself
#: succeeded; `failed` is the only losing outcome.
AGENT_SUCCEEDED_STATUSES: Final[tuple[str, ...]] = ("completed", "needs_review")
AGENT_OUTCOME_STATUSES: Final[tuple[str, ...]] = ("completed", "needs_review", "failed")
#: A denied call is a refused escalation attempt, which is a failed call for measurement
#: purposes - excluding it would let an Agent that tries to exceed its tools look healthier.
TOOL_FAILED_STATUSES: Final[tuple[str, ...]] = ("failed", "denied")
TOOL_SUCCEEDED_STATUSES: Final[tuple[str, ...]] = ("success",)
#: The report state behind ARCHITECTURE §12's "常规报告人工阻塞率".
MANUAL_BLOCK_STATUS: Final[str] = "pending_review"


def quantize_rate(value: Decimal) -> Decimal:
    """Round a ratio exactly the way the evaluation layer does."""
    return value.quantize(RATE_PLACES, rounding=ROUND_HALF_UP)


def as_rate(value: Decimal | None) -> str | None:
    """Render a ratio for JSON without inventing a number for "not measured"."""
    return None if value is None else str(value)


def success_rate(*, succeeded: int, denominator: int) -> Decimal | None:
    """Return `succeeded / denominator`, or `None` when nothing reached an outcome."""
    if denominator <= 0:
        return None
    return quantize_rate(Decimal(succeeded) / Decimal(denominator))


def mean_decimal(values: Sequence[Decimal]) -> Decimal | None:
    """Return the quantized mean, or `None` for an empty sample."""
    if not values:
        return None
    return quantize_rate(sum(values) / Decimal(len(values)))


def percentile(values: Sequence[int], percent: int) -> int | None:
    """Return a nearest-rank percentile, or `None` for an empty sample.

    Nearest rank is used instead of an interpolating definition because it always returns a
    duration that some run actually took. An operator reading "P95 latency" wants a real
    worst-ish case, not a synthetic midpoint between two runs.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percent / 100 * len(ordered)))
    return ordered[rank - 1]


def count_statuses(statuses: Iterable[str]) -> dict[str, int]:
    """Count rows per status; an unobserved status is absent rather than zero."""
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def summarize_latency(values: Sequence[int]) -> Latency:
    """Build a percentile summary from a duration sample measured in milliseconds."""
    return Latency(
        samples=len(values),
        p50_ms=percentile(values, 50),
        p95_ms=percentile(values, 95),
    )


@dataclass(frozen=True, slots=True)
class Window:
    """The half-open time span every metric in one response was computed over."""

    days: int
    start: datetime
    end: datetime

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload."""
        return {"days": self.days, "start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True, slots=True)
class Latency:
    """Percentiles of one duration sample, plus how many observations backed them."""

    samples: int
    p50_ms: int | None
    p95_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload."""
        return {"samples": self.samples, "p50_ms": self.p50_ms, "p95_ms": self.p95_ms}


@dataclass(frozen=True, slots=True)
class PipelineMetrics:
    """Throughput and health of the scheduled pipeline tasks."""

    total: int
    by_status: dict[str, int]
    by_run_type: dict[str, dict[str, int]]
    success_rate: Decimal | None
    latency: Latency

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload."""
        return {
            "total": self.total,
            "by_status": self.by_status,
            "by_run_type": self.by_run_type,
            "success_rate": as_rate(self.success_rate),
            "latency": self.latency.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class AgentMetrics:
    """Budget consumption, outcome, and failure reasons of the research Agent."""

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
        """Return a JSON-safe payload."""
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
    """Per-tool call counts and latency, so one misbehaving tool is visible on its own."""

    tool_name: str
    total: int
    by_status: dict[str, int]
    success_rate: Decimal | None
    latency: Latency

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload."""
        return {
            "tool_name": self.tool_name,
            "total": self.total,
            "by_status": self.by_status,
            "success_rate": as_rate(self.success_rate),
            "latency": self.latency.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ToolMetrics:
    """Whitelisted tool usage, ordered so the busiest tool comes first."""

    total: int
    by_status: dict[str, int]
    success_rate: Decimal | None
    by_tool: tuple[ToolBreakdown, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload."""
        return {
            "total": self.total,
            "by_status": self.by_status,
            "success_rate": as_rate(self.success_rate),
            "by_tool": [item.to_dict() for item in self.by_tool],
        }


@dataclass(frozen=True, slots=True)
class ReportMetrics:
    """Report production and how much of it needs a human before it can go out."""

    total: int
    by_status: dict[str, int]
    manual_block_rate: Decimal | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload."""
        return {
            "total": self.total,
            "by_status": self.by_status,
            "manual_block_rate": as_rate(self.manual_block_rate),
        }


@dataclass(frozen=True, slots=True)
class EvaluationTarget:
    """One variant of the newest evaluation sweep, carried over from the evaluation layer."""

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
        """Build a dashboard row from the evaluation layer's own summary object."""
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
        """Return a JSON-safe payload."""
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
    """The newest evaluation sweep, reused verbatim rather than recomputed.

    The dashboard never re-derives `passed`: it comes from `evaluation.runner.compare_latest`.
    A green dashboard therefore cannot disagree with the evaluation report it is summarising,
    and the two can never drift apart as the verdict rule changes.

    Unlike the other sections this one is deliberately **not** windowed. Evaluations are
    operator-triggered and infrequent, so a 1-day window would usually show nothing; the newest
    sweep is the useful thing to surface.
    """

    dataset_id: int | None
    dataset_name: str | None
    dataset_version: str | None
    passed: bool | None
    regressions: tuple[str, ...]
    targets: tuple[EvaluationTarget, ...]

    @property
    def available(self) -> bool:
        """Return whether any evaluation sweep has ever been recorded."""
        return self.dataset_id is not None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload."""
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
    """The whole dashboard payload: one window, five sections, no writes."""

    window: Window
    pipeline: PipelineMetrics
    agent: AgentMetrics
    tools: ToolMetrics
    reports: ReportMetrics
    evaluations: EvaluationMetrics

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload."""
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
