"""Run a dataset against target variants and assemble the unified comparison report.

The runner is the piece that answers "did the Agent earn its complexity". It executes each
requested variant over the same frozen cases, scores every case with the deterministic scorers,
persists one `eval_runs` row per variant plus one `eval_results` row per case, and then builds a
single comparison so the variants can be read side by side.

Two decisions worth naming:

* **Averages skip `None`.** A metric that a case never exercised is not a zero, and averaging
  it in as one would make a narrower ablation look worse than it is. Counts of scored versus
  skipped cases are reported so the reader can see how thin a mean is.
* **Every variant is measured against the variant it is a change to, not against one fixed
  reference.** The comparison therefore forms a chain: `baseline_single_prompt <- agent_full <-
  agent_no_*`. The Agent answers `docs/evaluation.md` §9's promotion question ("is it worth
  shipping over doing nothing?"), while each ablation answers §8's ablation question ("what does
  removing this capability cost?"). Measuring an ablation against the baseline would hide the
  loss entirely, because the baseline is already at the floor the ablation falls to.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.llm.base import ModelClient
from open_source_intelligence.agent.profile import profile_for_target
from open_source_intelligence.agent.runtime import load_or_create_config
from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.agent import AgentConfig
from open_source_intelligence.persistence.models.evaluation import (
    EVAL_TARGET_TYPES,
    EvalCase,
    EvalDataset,
    EvalResult,
    EvalRun,
)

from .dataset import list_cases, list_datasets, resolve_dataset
from .errors import EvaluationDisabled, EvaluationNotFound, UnknownTarget
from .scorers import ScoredCase, score_case, score_failed_case
from .targets import AGENT_TARGET, BASELINE_TARGET, TARGET_ORDER, VariantOutput, run_target

#: Metrics that can block a promotion. `docs/evaluation.md` §8 names exactly these three.
GATED_METRICS: Final[tuple[str, ...]] = (
    "citation_coverage",
    "factual_consistency",
    "task_completion",
)

#: Targets whose result decides promotion (`docs/evaluation.md` §9). An ablation measures what a
#: capability is worth and is *expected* to lose, so it can never veto a release; its damage is
#: reported through its own deltas and gate failures instead.
PROMOTION_TARGETS: Final[tuple[str, ...]] = (AGENT_TARGET,)

#: Absolute acceptance targets from `ARCHITECTURE.md` §12.
METRIC_TARGETS: Final[dict[str, Decimal]] = {
    "citation_coverage": Decimal("0.95"),
    "factual_consistency": Decimal("0.90"),
    "task_completion": Decimal("0.90"),
}

#: Rates promoted to real `eval_results` columns, because they gate a release.
COLUMN_METRICS: Final[tuple[str, ...]] = (
    "citation_coverage",
    "factual_consistency",
    "task_completion",
)

#: Rates that have no threshold yet and therefore ride along in `details_json`. Keeping them
#: out of the schema means adding a measured rate never requires a migration.
DETAIL_METRICS: Final[tuple[str, ...]] = (
    "source_accuracy",
    "tool_success_rate",
)

#: Every rate that gets averaged into a run summary.
RATE_METRICS: Final[tuple[str, ...]] = COLUMN_METRICS + DETAIL_METRICS

RATE_PLACES: Final[Decimal] = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class TargetSummary:
    """One variant's aggregated outcome, and how it compares to its reference variant."""

    target_type: str
    run_id: int
    status: str
    case_count: int
    scored_case_count: int
    failed_case_count: int
    metrics: dict[str, Decimal | None]
    total_cost: Decimal
    total_tokens: int
    mean_latency_ms: int
    prompt_injection_block_rate: Decimal | None
    tool_call_total: int = 0
    tool_call_denied: int = 0
    verification_sufficient_rate: Decimal | None = None
    reference_target: str | None = None
    deltas_vs_reference: dict[str, Decimal | None] = field(default_factory=dict)
    gate_failures: list[str] = field(default_factory=list)
    regression: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload, stored on `eval_runs.summary_json`."""
        return {
            "target_type": self.target_type,
            "run_id": self.run_id,
            "status": self.status,
            "case_count": self.case_count,
            "scored_case_count": self.scored_case_count,
            "failed_case_count": self.failed_case_count,
            "metrics": {
                name: None if value is None else str(value) for name, value in self.metrics.items()
            },
            "total_cost": str(self.total_cost),
            "total_tokens": self.total_tokens,
            "mean_latency_ms": self.mean_latency_ms,
            "prompt_injection_block_rate": (
                None
                if self.prompt_injection_block_rate is None
                else str(self.prompt_injection_block_rate)
            ),
            "tool_call_total": self.tool_call_total,
            "tool_call_denied": self.tool_call_denied,
            "verification_sufficient_rate": (
                None
                if self.verification_sufficient_rate is None
                else str(self.verification_sufficient_rate)
            ),
            "reference_target": self.reference_target,
            "deltas_vs_reference": {
                name: None if value is None else str(value)
                for name, value in self.deltas_vs_reference.items()
            },
            "gate_failures": list(self.gate_failures),
            "regression": self.regression,
        }


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """The unified report: every requested variant, plus the promotion verdict."""

    dataset_id: int
    dataset_name: str
    dataset_version: str
    case_count: int
    targets: tuple[TargetSummary, ...]
    replayed: bool = False

    @property
    def passed(self) -> bool:
        """Return the promotion verdict for the configuration under evaluation.

        This is `docs/evaluation.md` §9's "是否允许进入生产的结论": did the Agent clear its
        absolute targets and avoid regressing against the baseline? Ablations are excluded
        because they are controls, not candidates - a control that scores badly is the point of
        the experiment. A sweep that contains no candidate (a baseline-only or ablation-only
        diagnostic run) is judged on every variant it does contain.

        A variant that produced no scorable case cannot pass. Every gate is a threshold on a
        measurement, and an unmeasured variant has none, so a run whose cases all failed would
        otherwise sail through with an empty `gate_failures` - the most dangerous possible
        outcome for a release gate.
        """
        considered = [item for item in self.targets if item.target_type in PROMOTION_TARGETS]
        if not considered:
            considered = list(self.targets)
        if not considered:
            return False
        return all(
            not item.regression and not item.gate_failures and item.scored_case_count > 0
            for item in considered
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload for the CLI and the API."""
        return {
            "dataset": {
                "id": self.dataset_id,
                "name": self.dataset_name,
                "version": self.dataset_version,
            },
            "case_count": self.case_count,
            "targets": [summary.to_dict() for summary in self.targets],
            "passed": self.passed,
            "regressions": [summary.target_type for summary in self.targets if summary.regression],
            "failed_gates": {
                summary.target_type: list(summary.gate_failures)
                for summary in self.targets
                if summary.gate_failures
            },
        }


def _quantize(value: Decimal) -> Decimal:
    """Quantize a derived rate to the result-column scale."""
    return value.quantize(RATE_PLACES, rounding=ROUND_HALF_UP)


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    """Return the quantized mean of the values, or None when there are none."""
    if not values:
        return None
    return _quantize(sum(values) / Decimal(len(values)))


def _as_decimal(value: Any) -> Decimal | None:
    """Coerce a `details_json` rate back to Decimal, treating anything unusable as absent."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except ArithmeticError:  # pragma: no cover - malformed JSON payload
            return None
    return None


def _detail_total(rows: Sequence[EvalResult], field: str) -> int:
    """Sum one integer counter that lives in `details_json`."""
    return sum(int((row.details_json or {}).get(field) or 0) for row in rows)


def _closed_at(started_at: datetime, requested: datetime | None) -> datetime:
    """Return a finish timestamp that cannot precede the start.

    `eval_runs` enforces `finished_at >= started_at`, and callers may pin the clock, so a run
    that was started at a supplied instant has to be closed no earlier than that instant.
    """
    closed = requested or utc_now()
    return closed if closed >= started_at else started_at


def resolve_targets(target_types: Sequence[str] | None) -> tuple[str, ...]:
    """Return the requested targets in report order, defaulting to every target."""
    if not target_types:
        return TARGET_ORDER
    unknown = [name for name in target_types if name not in EVAL_TARGET_TYPES]
    if unknown:
        raise UnknownTarget(
            f"unknown eval target(s): {', '.join(unknown)}; "
            f"expected one of {', '.join(EVAL_TARGET_TYPES)}"
        )
    ordered = [name for name in TARGET_ORDER if name in set(target_types)]
    return tuple(ordered)


def resolve_dataset_ref(
    session: Session,
    *,
    dataset_id: int | None = None,
    name: str | None = None,
    version: str | None = None,
) -> EvalDataset:
    """Resolve a dataset from either an id or a name/version pair."""
    return resolve_dataset(session, dataset_id=dataset_id, name=name, version=version)


def _record_result(
    session: Session,
    *,
    run_id: int,
    case: EvalCase,
    scored: ScoredCase,
) -> None:
    """Insert or refresh one case measurement.

    `UNIQUE(eval_run_id, eval_case_id)` makes a re-run of the same case an update rather than a
    second row, so an interrupted run that is retried cannot double-count a case.
    """
    row = session.scalar(
        select(EvalResult).where(
            EvalResult.eval_run_id == run_id,
            EvalResult.eval_case_id == case.id,
        )
    )
    if row is None:
        row = EvalResult(eval_run_id=run_id, eval_case_id=case.id)
        session.add(row)
    row.status = scored.status
    row.citation_coverage = scored.citation_coverage
    row.factual_consistency = scored.factual_consistency
    row.task_completion = scored.task_completion
    row.cost = scored.cost
    row.latency_ms = scored.latency_ms
    row.details_json = scored.to_dict()
    session.flush()


def _config_id_for_target(
    session: Session,
    *,
    target_type: str,
    settings: Settings,
) -> int | None:
    """Return the Agent config id for an agent-family target, or None for the baseline."""
    if profile_for_target(target_type) is None:
        return None
    config: AgentConfig = load_or_create_config(session, settings)
    return config.id


def run_evaluation(
    session: Session,
    *,
    dataset_id: int | None = None,
    name: str | None = None,
    version: str | None = None,
    target_types: Sequence[str] | None = None,
    settings: Settings | None = None,
    client: ModelClient | None = None,
    now: datetime | None = None,
) -> ComparisonReport:
    """Run every requested target over one dataset and return the comparison report."""
    resolved_settings = settings or get_settings()
    if not resolved_settings.eval_enabled:
        raise EvaluationDisabled("evaluation is disabled by configuration")
    dataset = resolve_dataset_ref(
        session,
        dataset_id=dataset_id,
        name=name,
        version=version,
    )
    selected = resolve_targets(target_types)
    cases = list(list_cases(session, dataset_id=dataset.id, enabled_only=True))
    if not cases:
        raise EvaluationNotFound(
            f"eval dataset {dataset.name} version {dataset.version} has no enabled case"
        )

    run_ids: list[int] = []
    for target_type in selected:
        started_at = now or utc_now()
        run = EvalRun(
            dataset_id=dataset.id,
            target_type=target_type,
            status="running",
            started_at=started_at,
            agent_config_id=_config_id_for_target(
                session,
                target_type=target_type,
                settings=resolved_settings,
            ),
        )
        session.add(run)
        session.commit()
        run_ids.append(run.id)
        failures: list[str] = []
        for case in cases:
            try:
                output: VariantOutput = run_target(
                    session,
                    case=case,
                    target_type=target_type,
                    settings=resolved_settings,
                    client=client,
                    now=now,
                )
            except Exception as exc:  # noqa: BLE001 - one broken case must not kill the run
                # A case that raises is a data or provider problem, not a quality signal. It is
                # recorded as a failed result so the run still finishes and the operator can see
                # exactly which case broke instead of losing the whole sweep.
                failures.append(case.case_key)
                scored = score_failed_case(
                    case_key=case.case_key,
                    status="failed",
                    error_code="EVAL_CASE_ERROR",
                    error_message=f"{exc.__class__.__name__}: {exc}"[:500],
                )
            else:
                if output.succeeded:
                    scored = score_case(session, case=case, output=output)
                else:
                    failures.append(case.case_key)
                    scored = score_failed_case(
                        case_key=case.case_key,
                        status=output.status,
                        error_code=output.error_code,
                        error_message=output.error_message,
                    )
            _record_result(session, run_id=run.id, case=case, scored=scored)
        session.commit()

    report = build_comparison(session, run_ids=run_ids, settings=resolved_settings)
    session.commit()
    return report


def aggregate_run(session: Session, *, run: EvalRun) -> TargetSummary:
    """Average one run's stored results into a summary."""
    rows = list(
        session.scalars(
            select(EvalResult).where(EvalResult.eval_run_id == run.id).order_by(EvalResult.id.asc())
        )
    )
    metrics: dict[str, Decimal | None] = {}
    for metric in RATE_METRICS:
        values: list[Decimal] = []
        for row in rows:
            if metric in COLUMN_METRICS:
                candidate = getattr(row, metric)
            else:
                candidate = _as_decimal((row.details_json or {}).get(metric))
            if isinstance(candidate, Decimal):
                values.append(candidate)
        metrics[metric] = _mean(values)
    scored_rows = [row for row in rows if row.status != "failed"]
    injection_flags = [
        bool((row.details_json or {}).get("prompt_injection_blocked"))
        for row in rows
        if (row.details_json or {}).get("prompt_injection_blocked") is not None
    ]
    # Run-level tool and verification signals. `docs/evaluation.md` §8 asks what removing a
    # capability costs, and for some capabilities the cost shows up here rather than in a rate:
    # `agent_no_tools` is recognisable by having read nothing, `agent_no_history` by its denied
    # prior-report call, and `agent_no_verifier` by never failing a verification.
    verification_flags = [
        bool((row.details_json or {}).get("verification_sufficient"))
        for row in rows
        if (row.details_json or {}).get("verification_sufficient") is not None
    ]
    latency_rows = [row.latency_ms for row in rows if row.latency_ms is not None]
    return TargetSummary(
        target_type=run.target_type,
        run_id=run.id,
        status=run.status,
        case_count=len(rows),
        scored_case_count=len(scored_rows),
        failed_case_count=len(rows) - len(scored_rows),
        metrics=metrics,
        total_cost=sum((row.cost for row in rows), Decimal("0")),
        total_tokens=_detail_total(rows, "total_tokens"),
        mean_latency_ms=int(sum(latency_rows) / len(latency_rows)) if latency_rows else 0,
        prompt_injection_block_rate=_mean(
            [Decimal("1") if flag else Decimal("0") for flag in injection_flags]
        ),
        tool_call_total=_detail_total(rows, "tool_call_total"),
        tool_call_denied=_detail_total(rows, "tool_call_denied"),
        verification_sufficient_rate=_mean(
            [Decimal("1") if flag else Decimal("0") for flag in verification_flags]
        ),
    )


def _tolerance(settings: Settings) -> Decimal:
    """Return the configured regression tolerance as a rate."""
    return Decimal(str(settings.eval_regression_tolerance))


def _gate_failures(summary: TargetSummary) -> list[str]:
    """Return the absolute acceptance targets a variant missed."""
    failures: list[str] = []
    for metric in GATED_METRICS:
        value = summary.metrics.get(metric)
        target = METRIC_TARGETS[metric]
        if value is not None and value < target:
            failures.append(f"{metric} {value} < target {target}")
    return failures


def _reference_summary(
    summary: TargetSummary,
    summaries: Sequence[TargetSummary],
) -> TargetSummary | None:
    """Return the variant a target is a change to, or None when it has no reference.

    The chain is `baseline <- agent_full <- ablations`. The Agent is measured against doing
    nothing; an ablation is measured against the Agent whose capability it removed. Comparing an
    ablation against the baseline instead would report a zero delta for exactly the ablations
    that hurt most, since the baseline already sits at the floor they fall to.
    """
    if summary.target_type == BASELINE_TARGET:
        return None
    by_type = {item.target_type: item for item in summaries}
    if summary.target_type in PROMOTION_TARGETS:
        return by_type.get(BASELINE_TARGET)
    return by_type.get(AGENT_TARGET, by_type.get(BASELINE_TARGET))


def _deltas(
    summary: TargetSummary,
    reference: TargetSummary | None,
) -> dict[str, Decimal | None]:
    """Return each gated metric's change against the reference variant."""
    if reference is None:
        return {}
    deltas: dict[str, Decimal | None] = {}
    for metric in GATED_METRICS:
        current = summary.metrics.get(metric)
        expected = reference.metrics.get(metric)
        if current is None or expected is None:
            deltas[metric] = None
        else:
            deltas[metric] = _quantize(current - expected)
    return deltas


def _regressed(deltas: dict[str, Decimal | None], tolerance: Decimal) -> bool:
    """Return whether any gated metric fell further than the tolerance allows."""
    return any(value is not None and value < -tolerance for value in deltas.values())


def build_comparison(
    session: Session,
    *,
    run_ids: Sequence[int],
    settings: Settings | None = None,
    dataset: EvalDataset | None = None,
) -> ComparisonReport:
    """Assemble the unified comparison over the given runs and persist each run's summary."""
    resolved_settings = settings or get_settings()
    tolerance = _tolerance(resolved_settings)
    runs = [run for run in (session.get(EvalRun, run_id) for run_id in run_ids) if run is not None]
    if not runs:
        raise EvaluationNotFound("no eval run matched the requested ids")
    resolved_dataset = dataset
    if resolved_dataset is None:
        resolved_dataset = resolve_dataset(session, dataset_id=runs[0].dataset_id)

    ordered = sorted(
        runs,
        key=lambda run: (
            TARGET_ORDER.index(run.target_type)
            if run.target_type in TARGET_ORDER
            else len(TARGET_ORDER),
            run.id,
        ),
    )
    summaries = [aggregate_run(session, run=run) for run in ordered]
    finalized: list[TargetSummary] = []
    for summary, run in zip(summaries, ordered, strict=True):
        # The terminal status is decided here, before the summary is frozen, so the row and its
        # own summary agree. A run whose every case failed is "failed"; a partial failure stays
        # "succeeded" with `failed_case_count` recording the misses.
        status = summary.status
        if run.status == "running":
            status = "failed" if summary.failed_case_count == summary.case_count else "succeeded"
        reference = _reference_summary(summary, summaries)
        deltas = _deltas(summary, reference)
        final = TargetSummary(
            target_type=summary.target_type,
            run_id=summary.run_id,
            status=status,
            case_count=summary.case_count,
            scored_case_count=summary.scored_case_count,
            failed_case_count=summary.failed_case_count,
            metrics=summary.metrics,
            total_cost=summary.total_cost,
            total_tokens=summary.total_tokens,
            mean_latency_ms=summary.mean_latency_ms,
            prompt_injection_block_rate=summary.prompt_injection_block_rate,
            tool_call_total=summary.tool_call_total,
            tool_call_denied=summary.tool_call_denied,
            verification_sufficient_rate=summary.verification_sufficient_rate,
            reference_target=None if reference is None else reference.target_type,
            deltas_vs_reference=deltas,
            gate_failures=_gate_failures(summary),
            regression=_regressed(deltas, tolerance),
        )
        finalized.append(final)
        # The summary lives on the row so the API can answer "what did this run conclude" without
        # re-executing anything, and so an operator reading one row sees the same numbers the
        # comparison report shows.
        run.status = status
        run.summary_json = final.to_dict()
        run.finished_at = _closed_at(run.started_at or utc_now(), run.finished_at)
    session.commit()
    return ComparisonReport(
        dataset_id=resolved_dataset.id,
        dataset_name=resolved_dataset.name,
        dataset_version=resolved_dataset.version,
        case_count=len(list_cases(session, dataset_id=resolved_dataset.id, enabled_only=True)),
        targets=tuple(finalized),
    )


def latest_runs_per_target(session: Session, *, dataset_id: int) -> list[EvalRun]:
    """Return the newest run for each target of one dataset."""
    rows = session.scalars(
        select(EvalRun).where(EvalRun.dataset_id == dataset_id).order_by(EvalRun.id.asc())
    ).all()
    latest: dict[str, EvalRun] = {}
    for row in rows:
        latest[row.target_type] = row
    return [latest[name] for name in TARGET_ORDER if name in latest]


def list_runs(
    session: Session,
    *,
    dataset_id: int | None = None,
    target_type: str | None = None,
    limit: int = 50,
) -> Sequence[EvalRun]:
    """Return eval runs, newest first."""
    if limit < 1:
        raise EvaluationNotFound("limit must be at least 1")
    statement = select(EvalRun)
    if dataset_id is not None:
        statement = statement.where(EvalRun.dataset_id == dataset_id)
    if target_type is not None:
        statement = statement.where(EvalRun.target_type == target_type)
    return session.scalars(statement.order_by(EvalRun.id.desc()).limit(limit)).all()


def compare_latest(
    session: Session,
    *,
    dataset_id: int,
    settings: Settings | None = None,
) -> ComparisonReport:
    """Build the comparison over the newest run of each target without re-running anything."""
    runs = latest_runs_per_target(session, dataset_id=dataset_id)
    if not runs:
        raise EvaluationNotFound(f"eval dataset {dataset_id} has no run yet")
    return build_comparison(
        session,
        run_ids=[run.id for run in runs],
        settings=settings,
    )


def collect_datasets(session: Session, *, limit: int = 50) -> Sequence[EvalDataset]:
    """Return known datasets, newest first."""
    return list_datasets(session, limit=limit)


__all__ = [
    "GATED_METRICS",
    "METRIC_TARGETS",
    "PROMOTION_TARGETS",
    "ComparisonReport",
    "TargetSummary",
    "aggregate_run",
    "build_comparison",
    "collect_datasets",
    "compare_latest",
    "latest_runs_per_target",
    "list_runs",
    "resolve_dataset_ref",
    "resolve_targets",
    "run_evaluation",
]
