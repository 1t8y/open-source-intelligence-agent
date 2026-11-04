"""在一份数据集上运行各目标变体，并组装统一的对比报告。

运行器就是回答「Agent 值不值它这份复杂度」的那块代码。它在同一批冻结的用例上执行每个被
请求的变体，用确定性打分器给每个用例打分，为每个变体落一行 `eval_runs`、为每个用例落一行
`eval_results`，然后组装出一份统一的对比，让各变体可以并排阅读。

有两项决定值得点名：

* **求均值时跳过 `None`。** 某个用例从未行使过的指标不是零，把它按零平均进去，会让范围更
  窄的消融变体显得比实际更差。打分与跳过的用例数都会报出来，读者因此能看出一个均值有多薄。
* **每个变体都对照「它所改动的那一个」来量，而不是对照某个固定参照。** 对比因此串成一条链：
  `baseline_single_prompt <- agent_full <- agent_no_*`。Agent 回答的是
  `docs/evaluation.md` §9 的晋升问题（「比起什么都不做，它值得上线吗？」），而每个消融变体
  回答的是 §8 的消融问题（「去掉这项能力要付什么代价？」）。拿消融变体去对照基线，损失会
  被完全藏住，因为基线本来就处在消融变体跌落到的那个地板上。
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
from open_source_intelligence.core.logging import redact_text
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

#: 能挡住一次晋升的指标。`docs/evaluation.md` §8 点名的正是这三项。
GATED_METRICS: Final[tuple[str, ...]] = (
    "citation_coverage",
    "factual_consistency",
    "task_completion",
)

#: 结果能决定晋升的目标（`docs/evaluation.md` §9）。消融变体量的是某项能力的价值，*本来就
#: 该*输，所以它永远不能否决一次发布；它的损伤改由自身的差值与门禁失败来报告。
PROMOTION_TARGETS: Final[tuple[str, ...]] = (AGENT_TARGET,)

#: 来自 `ARCHITECTURE.md` §12 的绝对验收目标。
METRIC_TARGETS: Final[dict[str, Decimal]] = {
    "citation_coverage": Decimal("0.95"),
    "factual_consistency": Decimal("0.90"),
    "task_completion": Decimal("0.90"),
}

#: 提升为 `eval_results` 真实列的比率，因为它们把关发布。
COLUMN_METRICS: Final[tuple[str, ...]] = (
    "citation_coverage",
    "factual_consistency",
    "task_completion",
)

#: 尚无阈值、因而随 `details_json` 一起走的比率。把它们留在 schema 之外，意味着新增一个
#: 被测比率永远不需要迁移。
DETAIL_METRICS: Final[tuple[str, ...]] = (
    "source_accuracy",
    "tool_success_rate",
)

#: 会被平均进运行摘要的每一个比率。
RATE_METRICS: Final[tuple[str, ...]] = COLUMN_METRICS + DETAIL_METRICS

RATE_PLACES: Final[Decimal] = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class TargetSummary:
    """一个变体的聚合结果，以及它相对于其参照变体的表现。"""

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
        """返回可安全序列化为 JSON 的载荷，存到 `eval_runs.summary_json` 上。"""
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
    """统一报告：每个被请求的变体，加上晋升结论。"""

    dataset_id: int
    dataset_name: str
    dataset_version: str
    case_count: int
    targets: tuple[TargetSummary, ...]
    replayed: bool = False

    @property
    def passed(self) -> bool:
        """返回正在评测的这套配置的晋升结论。

        这就是 `docs/evaluation.md` §9 的「是否允许进入生产的结论」：Agent 有没有达到它的
        绝对目标、有没有相对基线发生退化？消融变体被排除在外，因为它们是对照组而非候选者
        —— 对照组得分难看，正是实验的意义所在。若一次批量里没有候选者（只跑基线或只跑消融
        的诊断运行），则按它实际包含的每个变体来判定。

        一个没有产出任何可打分用例的变体不可能通过。每道门禁都是某个测量值上的阈值，而没被
        测量的变体没有测量值，否则一次所有用例都失败的运行就会顶着空的 `gate_failures` 溜
        过去 —— 那对发布门禁来说是最危险的结果。
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
        """返回可安全序列化为 JSON 的载荷，供 CLI 与 API 使用。"""
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
    """把推导出的比率量化到结果列的精度。"""
    return value.quantize(RATE_PLACES, rounding=ROUND_HALF_UP)


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    """返回这些值的量化均值，一个值都没有时返回 None。"""
    if not values:
        return None
    return _quantize(sum(values) / Decimal(len(values)))


def _as_decimal(value: Any) -> Decimal | None:
    """把 `details_json` 里的比率转回 Decimal，无法使用的值一律当作缺失。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except ArithmeticError:  # pragma: no cover - 格式错误的 JSON 载荷
            return None
    return None


def _detail_total(rows: Sequence[EvalResult], field: str) -> int:
    """求和 `details_json` 里的某一个整数计数器。"""
    return sum(int((row.details_json or {}).get(field) or 0) for row in rows)


def _closed_at(started_at: datetime, requested: datetime | None) -> datetime:
    """返回一个不会早于开始时刻的结束时间戳。

    `eval_runs` 强制 `finished_at >= started_at`，而调用方可能钉住时钟，因此在某个给定时刻
    开始的运行，必须不早于那个时刻结束。
    """
    closed = requested or utc_now()
    return closed if closed >= started_at else started_at


def resolve_targets(target_types: Sequence[str] | None) -> tuple[str, ...]:
    """按报告顺序返回所请求的目标，未指定时默认全部目标。"""
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
    """按 id，或按名称/版本对解析一个数据集。"""
    return resolve_dataset(session, dataset_id=dataset_id, name=name, version=version)


def _record_result(
    session: Session,
    *,
    run_id: int,
    case: EvalCase,
    scored: ScoredCase,
) -> None:
    """插入或刷新一条用例的测量值。

    `UNIQUE(eval_run_id, eval_case_id)` 让同一个用例的重跑变成一次更新而不是多出一行，因此
    一次被中断后重试的运行不会重复统计某个用例。
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
    """返回 agent 系目标所用的 Agent 配置 id，基线则返回 None。"""
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
    """在一份数据集上运行每个被请求的目标，并返回对比报告。"""
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
            except Exception as exc:  # noqa: BLE001 - 一个用例坏掉不能拖垮整轮运行
                # 抛异常的用例是数据或供应商的问题，不是质量信号。它被记成一条失败结果，
                # 这样整轮仍能跑完，运维也能确切看到坏的是哪个用例，而不是丢掉整批。
                failures.append(case.case_key)
                scored = score_failed_case(
                    case_key=case.case_key,
                    status="failed",
                    error_code="EVAL_CASE_ERROR",
                    error_message=redact_text(f"{exc.__class__.__name__}: {exc}")[:500],
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
    """把一次运行已存的结果平均成摘要。"""
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
    # 运行级的工具与验证信号。`docs/evaluation.md` §8 问的是去掉一项能力要付什么代价，而对
    # 某些能力来说，代价出现在这里而不是某个比率里：`agent_no_tools` 的特征是什么都没读，
    # `agent_no_history` 的特征是那次被拒绝的历史报告调用，`agent_no_verifier` 的特征则是
    # 从不曾让一次验证失败。
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
    """把配置里的回归容忍度作为比率返回。"""
    return Decimal(str(settings.eval_regression_tolerance))


def _gate_failures(summary: TargetSummary) -> list[str]:
    """返回某个变体未达到的绝对验收目标。"""
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
    """返回某个目标所改动的那一个变体，没有参照时返回 None。

    链条是 `baseline <- agent_full <- ablations`。Agent 相对「什么都不做」来量；一个消融
    变体则相对被它剪掉能力的那一个 Agent 来量。反过来拿消融变体去对照基线，恰恰会在伤害
    最大的那几个消融上报告零差值，因为基线本来就处在它们跌落到的那个地板上。
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
    """返回每个把关指标相对参照变体的变化量。"""
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
    """返回是否有某个把关指标的下滑超过了容忍度允许的范围。"""
    return any(value is not None and value < -tolerance for value in deltas.values())


def build_comparison(
    session: Session,
    *,
    run_ids: Sequence[int],
    settings: Settings | None = None,
    dataset: EvalDataset | None = None,
    persist: bool = True,
) -> ComparisonReport:
    """在给定的这几次运行上组装统一的对比。

    带上 `persist`（默认值，`run_evaluation` 用的就是它）时，每次运行冻结后的摘要与终态都会
    写回它对应的行。读路径传 `persist=False`：算一次对比是查询，而一个会改写
    `eval_runs.status` 的查询，会把每次仪表盘刷新都变成一次可能重判历史结论的写入。
    """
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
        # 终态在这里定下，早于摘要被冻结，这样行与它自己的摘要不会互相矛盾。每个用例都失败
        # 的运行是 `failed`；部分失败仍是 `succeeded`，漏掉的用例由 `failed_case_count`
        # 记录。
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
        # 摘要挂在行上，API 才能在不重跑任何东西的前提下回答「这次运行得出了什么结论」，
        # 运维读一行看到的数字，也和对比报告里显示的一致。
        if persist:
            run.status = status
            run.summary_json = final.to_dict()
            run.finished_at = _closed_at(run.started_at or utc_now(), run.finished_at)
    if persist:
        session.commit()
    return ComparisonReport(
        dataset_id=resolved_dataset.id,
        dataset_name=resolved_dataset.name,
        dataset_version=resolved_dataset.version,
        case_count=len(list_cases(session, dataset_id=resolved_dataset.id, enabled_only=True)),
        targets=tuple(finalized),
    )


def latest_runs_per_target(session: Session, *, dataset_id: int) -> list[EvalRun]:
    """返回一份数据集每个目标各自最新的一次运行。"""
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
    """返回评测运行，最新的在前。"""
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
    """在每个目标最新一次运行上组装对比，不写入任何东西。

    仪表盘与只读的对比接口调用的就是它，所以它绝不能落库：一次会重判 `eval_runs.status` 的
    页面刷新，会让「上周那次运行得出了什么结论」随看的人、看的时间不同而给出不同答案。
    """
    runs = latest_runs_per_target(session, dataset_id=dataset_id)
    if not runs:
        raise EvaluationNotFound(f"eval dataset {dataset_id} has no run yet")
    return build_comparison(
        session,
        run_ids=[run.id for run in runs],
        settings=settings,
        persist=False,
    )


def collect_datasets(session: Session, *, limit: int = 50) -> Sequence[EvalDataset]:
    """返回已知的数据集，最新的在前。"""
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
