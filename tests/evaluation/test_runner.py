"""守护执行器的执行、落库、汇总与提升闸门。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from evaluation_support import (
    GROWTH_TOPIC,
    WINDOW_END,
    build_case_document,
    build_dataset_document,
    import_shipped_dataset,
    seed_eval_world,
)
from sqlalchemy import select
from sqlalchemy.orm import Session
from support import Workspace, build_settings

from open_source_intelligence.evaluation.dataset import (
    import_dataset,
    list_cases,
    parse_dataset,
)
from open_source_intelligence.evaluation.errors import (
    EvaluationDisabled,
    EvaluationNotFound,
    UnknownTarget,
)
from open_source_intelligence.evaluation.runner import (
    GATED_METRICS,
    PROMOTION_TARGETS,
    TARGET_ORDER,
    aggregate_run,
    compare_latest,
    latest_runs_per_target,
    list_runs,
    resolve_targets,
    run_evaluation,
)
from open_source_intelligence.evaluation.targets import BASELINE_TARGET
from open_source_intelligence.evaluation.targets import TARGET_ORDER as ORDER
from open_source_intelligence.persistence.models.evaluation import (
    EVAL_TARGET_TYPES,
    EvalResult,
    EvalRun,
)
from open_source_intelligence.persistence.models.intelligence import Topic

SETTINGS = build_settings()

#: 五个目标的整轮扫描足够贵，值得在模块范围内复用；下面每个用例仍然自建数据库，
#: 所以这个常量只是省得反复推导那份列表。
ALL_TARGETS = TARGET_ORDER


def _seeded(session: Session, workspace: Workspace) -> int:
    """灌好世界并导入随附数据集，返回它的 id。"""
    seed_eval_world(session, workspace)
    return import_shipped_dataset(session)


def test_the_target_order_is_baseline_then_agent_then_ablations() -> None:
    """报告先读基线，再读完整 Agent，最后读各个消融版。"""
    assert ORDER == (
        "baseline_single_prompt",
        "agent_full",
        "agent_no_verifier",
        "agent_no_tools",
        "agent_no_history",
    )
    assert ALL_TARGETS == EVAL_TARGET_TYPES


def test_resolve_targets_defaults_to_every_variant() -> None:
    """省略选择时必须全跑，而不是一个都不跑。"""
    assert resolve_targets(None) == TARGET_ORDER
    assert resolve_targets([]) == TARGET_ORDER


def test_resolve_targets_keeps_report_order_not_request_order() -> None:
    """调用方可以按任意顺序请求，报告却始终可比。"""
    assert resolve_targets(["agent_no_tools", "baseline_single_prompt"]) == (
        "baseline_single_prompt",
        "agent_no_tools",
    )


def test_resolve_targets_refuses_an_unknown_variant() -> None:
    """写错名字不能悄悄只跑一个子集。"""
    with pytest.raises(UnknownTarget) as excinfo:
        resolve_targets(["agent_full", "agent_oops"])
    assert "agent_oops" in str(excinfo.value)


def test_a_full_sweep_produces_the_unified_report(session: Session, workspace: Workspace) -> None:
    """核心要求：每个变体都跑，并汇总进一份可比的报告。"""
    dataset_id = _seeded(session, workspace)
    report = run_evaluation(
        session,
        dataset_id=dataset_id,
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert report.dataset_id == dataset_id
    assert report.case_count == 3
    assert [summary.target_type for summary in report.targets] == list(TARGET_ORDER)

    baseline = report.targets[0]
    assert baseline.metrics["citation_coverage"] == Decimal("0.0000")
    assert baseline.metrics["source_accuracy"] is None
    assert baseline.metrics["tool_success_rate"] is None
    assert baseline.failed_case_count == 0
    # 基线只有一次模型调用、没有工具，所以没有可平均的东西。
    assert baseline.tool_call_total == 0
    assert baseline.verification_sufficient_rate is None

    full = report.targets[1]
    assert full.metrics["citation_coverage"] == Decimal("1.0000")
    assert full.metrics["task_completion"] == Decimal("1.0000")
    assert full.metrics["factual_consistency"] == Decimal("1.0000")
    # 并不精确等于 1.0：稀疏主题没有缓存 README，所以有一次内容读取合理地失败。
    # 工具成功率低于 `docs/evaluation.md` §3 的目标是一个发现，而不是缺陷，
    # 报告必须能把它显示出来。
    assert full.metrics["tool_success_rate"] is not None
    assert full.metrics["tool_success_rate"] > Decimal("0.90")
    assert full.metrics["source_accuracy"] is not None
    assert full.tool_call_total > 0


def test_the_agent_improves_citation_coverage_over_the_baseline(
    session: Session, workspace: Workspace
) -> None:
    """`docs/evaluation.md` §3 要求有可度量的提升，这里必须看得见。"""
    dataset_id = _seeded(session, workspace)
    report = run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    full = next(item for item in report.targets if item.target_type == "agent_full")
    assert full.reference_target == BASELINE_TARGET
    delta = full.deltas_vs_reference["citation_coverage"]
    assert delta is not None
    assert delta == Decimal("1.0000")
    assert delta > Decimal("0.10")
    assert full.regression is False


def test_an_ablation_is_measured_against_the_agent_not_the_baseline(
    session: Session, workspace: Workspace
) -> None:
    """§8 问的是某项能力值多少，所以差值必须是损失，而不是增益。

    对着基线算，`agent_no_tools` 的差值会是一个毫无意义的 `0.0000`，
    因为两个变体都没引用任何东西。对着 Agent 算，
    才是这个消融要展示的 `-1.0000`。
    """
    dataset_id = _seeded(session, workspace)
    report = run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    ablated = next(item for item in report.targets if item.target_type == "agent_no_tools")
    assert ablated.reference_target == "agent_full"
    assert ablated.metrics["citation_coverage"] == Decimal("0.0000")
    assert ablated.deltas_vs_reference["citation_coverage"] == Decimal("-1.0000")
    assert ablated.deltas_vs_reference["factual_consistency"] == Decimal("-1.0000")
    assert ablated.regression is True
    # 消融版回退，不能因此否决 Agent 自己的提升结论。
    assert report.passed is True
    assert "agent_no_tools" in report.to_dict()["regressions"]


def test_no_tools_reads_nothing(session: Session, workspace: Workspace) -> None:
    """消融必须能在报告里被辨认出来，而不只是体现在某个失败的指标上。"""
    dataset_id = _seeded(session, workspace)
    report = run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    ablated = next(item for item in report.targets if item.target_type == "agent_no_tools")
    full = next(item for item in report.targets if item.target_type == "agent_full")
    # 只剩下六次章节写入，所以工具活动严格少于完整 Agent 的研究流量。
    assert ablated.tool_call_total < full.tool_call_total
    # 从未主张过任何引用。
    assert ablated.metrics["source_accuracy"] is None
    # 什么都没读，也就没有注入面。
    assert ablated.prompt_injection_block_rate is None


def test_no_history_loses_the_prior_report_baseline(session: Session, workspace: Workspace) -> None:
    """丢掉历史的那次消融，不能与完整 Agent 无法区分。"""
    dataset_id = _seeded(session, workspace)
    report = run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    ablated = next(item for item in report.targets if item.target_type == "agent_no_history")
    full = next(item for item in report.targets if item.target_type == "agent_full")
    assert ablated.metrics["citation_coverage"] is not None
    assert full.metrics["citation_coverage"] is not None
    assert ablated.metrics["citation_coverage"] <= full.metrics["citation_coverage"]
    # 损害是具体的：每个用例的上期报告调用都被拒绝，
    # 而没有基线可比，覆盖闸门就永远无法满足。
    assert ablated.tool_call_denied == ablated.case_count
    assert ablated.verification_sufficient_rate == Decimal("0.0000")
    assert full.tool_call_denied == 0
    assert full.verification_sufficient_rate is not None
    assert full.verification_sufficient_rate > Decimal("0.0")


def test_no_verifier_keeps_the_draft_but_drops_the_gate(
    session: Session, workspace: Workspace
) -> None:
    """去掉校验器不能把草稿清零；它拿走的是闸门，不是能力。"""
    dataset_id = _seeded(session, workspace)
    report = run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    ablated = next(item for item in report.targets if item.target_type == "agent_no_verifier")
    # 六个章节都已起草。
    assert ablated.metrics["task_completion"] == Decimal("1.0000")
    assert ablated.metrics["citation_coverage"] == Decimal("0.0000")
    assert ablated.reference_target == "agent_full"
    assert ablated.regression is True
    # 校验仍然跑了、仍然给了结论 —— 只是永远说「充分」，
    # 而这正是这次消融要测其贡献的那道闸门。
    assert ablated.verification_sufficient_rate == Decimal("1.0000")
    full = next(item for item in report.targets if item.target_type == "agent_full")
    assert full.verification_sufficient_rate is not None
    assert full.verification_sufficient_rate < Decimal("1.0000")


def test_every_run_row_carries_its_own_summary(session: Session, workspace: Workspace) -> None:
    """每个变体一行，必须自己能回答「这个变体得出了什么结论」。"""
    dataset_id = _seeded(session, workspace)
    run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    rows = list_runs(session, dataset_id=dataset_id)
    assert len(rows) == len(TARGET_ORDER)
    for row in rows:
        assert row.summary_json is not None
        assert row.summary_json["target_type"] == row.target_type
        assert row.status == "succeeded"
        assert row.started_at is not None and row.finished_at is not None
        assert row.finished_at >= row.started_at
    agent_rows = [row for row in rows if row.target_type != BASELINE_TARGET]
    assert all(row.agent_config_id is not None for row in agent_rows)
    assert next(row for row in rows if row.target_type == BASELINE_TARGET).agent_config_id is None


def test_one_result_row_per_case_per_run(session: Session, workspace: Workspace) -> None:
    """`UNIQUE(eval_run_id, eval_case_id)` 正是让重试幂等的东西。"""
    dataset_id = _seeded(session, workspace)
    run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    for row in list_runs(session, dataset_id=dataset_id):
        results = session.scalars(select(EvalResult).where(EvalResult.eval_run_id == row.id)).all()
        assert len(results) == 3
        assert len({item.eval_case_id for item in results}) == 3


def test_rerunning_a_target_updates_rows_instead_of_appending(
    session: Session, workspace: Workspace
) -> None:
    """第二次扫描之后，结果表仍要保持每个用例每次运行一行。"""
    dataset_id = _seeded(session, workspace)
    run_evaluation(
        session,
        dataset_id=dataset_id,
        target_types=["agent_full"],
        settings=SETTINGS,
        now=WINDOW_END,
    )
    first_runs = latest_runs_per_target(session, dataset_id=dataset_id)
    assert len(first_runs) == 1

    run_evaluation(
        session,
        dataset_id=dataset_id,
        target_types=["agent_full"],
        settings=SETTINGS,
        now=WINDOW_END,
    )
    latest = latest_runs_per_target(session, dataset_id=dataset_id)
    # 只有一个变体，所以取它的最新一次运行。
    assert len(latest) == 1
    # 是新的一行运行记录，不是旧行被更新。
    assert latest[0].id != first_runs[0].id
    assert len(list_runs(session, dataset_id=dataset_id)) == 2
    for row in list_runs(session, dataset_id=dataset_id):
        assert (
            len(session.scalars(select(EvalResult).where(EvalResult.eval_run_id == row.id)).all())
            == 3
        )


def test_compare_latest_reproduces_the_report_without_rerunning(
    session: Session, workspace: Workspace
) -> None:
    """存下的汇总必须足以重建那份对比结果。"""
    dataset_id = _seeded(session, workspace)
    first = run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    runs_before = len(list_runs(session, dataset_id=dataset_id))
    again = compare_latest(session, dataset_id=dataset_id, settings=SETTINGS)
    assert len(list_runs(session, dataset_id=dataset_id)) == runs_before
    assert [item.target_type for item in again.targets] == [
        item.target_type for item in first.targets
    ]
    for before, after in zip(first.targets, again.targets, strict=True):
        assert before.metrics == after.metrics
        assert before.deltas_vs_reference == after.deltas_vs_reference
        assert before.reference_target == after.reference_target


def test_compare_latest_refuses_a_dataset_with_no_run(
    session: Session, workspace: Workspace
) -> None:
    """对比「无」必须是一个明确的错误，而不是一份空报告。"""
    dataset_id = _seeded(session, workspace)
    with pytest.raises(EvaluationNotFound):
        compare_latest(session, dataset_id=dataset_id, settings=SETTINGS)


def test_aggregate_run_skips_not_applicable_metrics(session: Session, workspace: Workspace) -> None:
    """变体从未触达的指标，均值必须仍是 None，而不能变成零。"""
    dataset_id = _seeded(session, workspace)
    run_evaluation(
        session,
        dataset_id=dataset_id,
        target_types=[BASELINE_TARGET],
        settings=SETTINGS,
        now=WINDOW_END,
    )
    run = latest_runs_per_target(session, dataset_id=dataset_id)[0]
    summary = aggregate_run(session, run=run)
    assert summary.metrics["citation_coverage"] == Decimal("0.0000")
    assert summary.metrics["source_accuracy"] is None
    assert summary.metrics["tool_success_rate"] is None
    assert summary.prompt_injection_block_rate is None
    assert summary.scored_case_count == 3
    assert summary.failed_case_count == 0
    assert summary.total_tokens == 0


def test_cases_with_a_missing_topic_are_counted_as_failures(
    session: Session, workspace: Workspace
) -> None:
    """坏掉的用例必须显形为失败，而不是悄悄打零分。

    数据集在导入时是合法的，之后它的主题被停用 —— 因为自 T25 起，
    点名一个谁都没有的主题的数据集在导入时就会被拒。
    「主题在导入与运行之间消失了」才是这道防线在生产上真能遇到的情形。
    """
    seed_eval_world(session, workspace)
    document = build_dataset_document(
        name="missing-topic",
        cases=[build_case_document(case_key="ghost", topic=GROWTH_TOPIC)],
    )
    dataset_id = _import_then_disable_its_topic(session, document)
    report = run_evaluation(
        session,
        dataset_id=dataset_id,
        target_types=[BASELINE_TARGET],
        settings=SETTINGS,
        now=WINDOW_END,
    )
    summary = report.targets[0]
    assert summary.status == "failed"
    assert summary.failed_case_count == 1
    assert summary.scored_case_count == 0
    assert summary.metrics["citation_coverage"] is None
    result = session.scalars(select(EvalResult)).one()
    assert result.status == "failed"
    assert result.details_json["error_code"] == "EVAL_TOPIC_NOT_FOUND"


def test_a_dataset_without_enabled_cases_is_refused(session: Session, workspace: Workspace) -> None:
    """跑一个空选择只会产出一份没有意义的报告。"""
    seed_eval_world(session, workspace)
    document = build_dataset_document(
        cases=[build_case_document(case_key="parked", enabled=False)],
    )
    dataset_id = import_dataset(session, parse_dataset(document)).dataset_id
    with pytest.raises(EvaluationNotFound):
        run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)


def test_evaluation_can_be_switched_off(session: Session, workspace: Workspace) -> None:
    """运维必须能在不碰代码的情况下关掉这个子系统。"""
    dataset_id = _seeded(session, workspace)
    disabled = build_settings(eval_enabled=False)
    with pytest.raises(EvaluationDisabled):
        run_evaluation(session, dataset_id=dataset_id, settings=disabled, now=WINDOW_END)


def test_the_regression_tolerance_is_configurable(session: Session, workspace: Workspace) -> None:
    """宽松的容差必须不再标记消融，这证明闸门确实读了配置。"""
    dataset_id = _seeded(session, workspace)
    loose = build_settings(eval_regression_tolerance=1.0)
    report = run_evaluation(session, dataset_id=dataset_id, settings=loose, now=WINDOW_END)
    ablated = next(item for item in report.targets if item.target_type == "agent_no_tools")
    assert ablated.regression is False


def test_list_runs_filters_by_target(session: Session, workspace: Workspace) -> None:
    """运行列表必须能按变体寻址。"""
    dataset_id = _seeded(session, workspace)
    run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    rows = list_runs(session, dataset_id=dataset_id, target_type="agent_full")
    assert [row.target_type for row in rows] == ["agent_full"]


def test_latest_runs_per_target_returns_the_newest_of_each(
    session: Session, workspace: Workspace
) -> None:
    """对比每个变体读一行，并且最新的那行胜出。"""
    dataset_id = _seeded(session, workspace)
    run_evaluation(
        session,
        dataset_id=dataset_id,
        target_types=["agent_full"],
        settings=SETTINGS,
        now=WINDOW_END,
    )
    run_evaluation(
        session,
        dataset_id=dataset_id,
        target_types=["agent_full"],
        settings=SETTINGS,
        now=WINDOW_END,
    )
    latest = latest_runs_per_target(session, dataset_id=dataset_id)
    assert len(latest) == 1
    all_runs = list_runs(session, dataset_id=dataset_id)
    assert latest[0].id == max(row.id for row in all_runs)


def test_gated_metrics_are_the_documented_three() -> None:
    """闸门必须恰好覆盖 `docs/evaluation.md` §8 点名的那几个指标。"""
    assert GATED_METRICS == ("citation_coverage", "factual_consistency", "task_completion")


def test_only_the_agent_decides_promotion() -> None:
    """对照组不能否决发布；候选与对照承担的是两种不同角色。"""
    assert PROMOTION_TARGETS == ("agent_full",)
    assert BASELINE_TARGET not in PROMOTION_TARGETS
    assert not any(name.startswith("agent_no") for name in PROMOTION_TARGETS)


def test_a_candidate_free_sweep_judges_every_variant_it_has(
    session: Session, workspace: Workspace
) -> None:
    """只跑基线的诊断轮没有候选，所以不能报一个空洞的「通过」。"""
    dataset_id = _seeded(session, workspace)
    report = run_evaluation(
        session,
        dataset_id=dataset_id,
        target_types=[BASELINE_TARGET],
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert [item.target_type for item in report.targets] == [BASELINE_TARGET]
    # 基线什么都没引用，于是达不到绝对目标，这一轮也就不算「通过」。
    assert report.passed is False
    assert report.to_dict()["failed_gates"][BASELINE_TARGET]


def test_the_report_serializes_to_json_safe_values(session: Session, workspace: Workspace) -> None:
    """载荷必须扛得住 `json.dumps`，因为 API 就是原样返回它的。"""
    import json

    dataset_id = _seeded(session, workspace)
    report = run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    payload = report.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["dataset"]["version"] == "v1"
    assert set(payload["targets"][1]["metrics"]) >= set(GATED_METRICS)


def test_runs_and_cases_stay_addressable_by_dataset(session: Session, workspace: Workspace) -> None:
    """两个数据集的运行与用例不能混在一起。"""
    first = _seeded(session, workspace)
    second = import_dataset(
        session,
        parse_dataset(
            build_dataset_document(name="second", cases=[build_case_document(case_key="x")])
        ),
    ).dataset_id
    run_evaluation(
        session,
        dataset_id=first,
        target_types=[BASELINE_TARGET],
        settings=SETTINGS,
        now=WINDOW_END,
    )
    run_evaluation(
        session,
        dataset_id=second,
        target_types=[BASELINE_TARGET],
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert len(list_cases(session, dataset_id=first)) == 3
    assert len(list_cases(session, dataset_id=second)) == 1
    first_runs = list_runs(session, dataset_id=first)
    second_runs = list_runs(session, dataset_id=second)
    assert {row.dataset_id for row in first_runs} == {first}
    assert {row.dataset_id for row in second_runs} == {second}
    assert {row.id for row in first_runs}.isdisjoint({row.id for row in second_runs})


def _import_then_disable_its_topic(session: Session, document: dict) -> int:
    """导入一份*此刻*合法的数据集，然后把它的主题停用。

    自 T25 起，导入会拒绝掉本部署没有对应主题的启用用例，
    所以数据集不能再一出生就是坏的。执行器仍然要防住主题在导入与运行之间
    消失（被删、被改名、被停用），这里造的就是这种状态，
    而不是导入器会拒绝的那一种。
    """
    dataset_id = import_dataset(session, parse_dataset(document)).dataset_id
    names = {case["topic"] for case in document["cases"] if case.get("enabled", True)}
    for topic in session.scalars(select(Topic).where(Topic.name.in_(sorted(names)))):
        topic.enabled = False
    session.commit()
    return dataset_id


def test_a_run_row_is_created_even_when_every_case_fails(
    session: Session, workspace: Workspace
) -> None:
    """整轮全败也必须留下证据，说明当时尝试了什么。"""
    seed_eval_world(session, workspace)
    document = build_dataset_document(
        name="all-broken",
        cases=[build_case_document(case_key="ghost-a", topic=GROWTH_TOPIC)],
    )
    dataset_id = _import_then_disable_its_topic(session, document)
    run_evaluation(
        session,
        dataset_id=dataset_id,
        target_types=[BASELINE_TARGET],
        settings=SETTINGS,
        now=WINDOW_END,
    )
    run = session.scalars(select(EvalRun)).one()
    assert run.status == "failed"
    assert run.finished_at is not None


def test_runner_redacts_provider_errors_before_persistence(
    session: Session,
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """供应商的异常文本可能带着凭据，绝不能原样存下来。"""
    dataset_id = _seeded(session, workspace)

    def broken_target(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("api_key=super-secret-provider-key")

    monkeypatch.setattr("open_source_intelligence.evaluation.runner.run_target", broken_target)
    run_evaluation(
        session,
        dataset_id=dataset_id,
        target_types=[BASELINE_TARGET],
        settings=SETTINGS,
        now=WINDOW_END,
    )
    result = session.scalars(select(EvalResult)).first()
    assert result is not None
    assert result.details_json is not None
    assert "super-secret-provider-key" not in str(result.details_json)


def test_a_wholly_failed_run_cannot_pass(session: Session, workspace: Workspace) -> None:
    """没有测量不等于测量通过。

    每道闸门都是分数上的阈值，所以一个用例全败的变体既没有分数、也没有
    未达标的阈值，于是 —— 没有这条规则的话 —— 会拿到一个干净的提升结论。
    对发布闸门来说，这是它最危险的出错方向。
    """
    seed_eval_world(session, workspace)
    document = build_dataset_document(
        name="all-broken-agent",
        cases=[build_case_document(case_key="ghost-a", topic=GROWTH_TOPIC)],
    )
    dataset_id = _import_then_disable_its_topic(session, document)
    report = run_evaluation(
        session,
        dataset_id=dataset_id,
        target_types=[BASELINE_TARGET, "agent_full"],
        settings=SETTINGS,
        now=WINDOW_END,
    )
    candidate = next(item for item in report.targets if item.target_type == "agent_full")
    assert candidate.scored_case_count == 0
    assert candidate.gate_failures == []
    assert report.passed is False


def test_comparing_is_a_read_and_does_not_rewrite_history(
    session: Session,
    workspace: Workspace,
) -> None:
    """构建对比的 GET 不能重新裁定 `eval_runs.status`。

    看板每次渲染都会调用 `compare_latest`。那段路径曾经会提交冻结的汇总与终态，
    于是每刷新一次页面，就把行改写一遍，并拿当时的数据重新裁定一个历史结论。
    """
    dataset_id = _seeded(session, workspace)
    run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)

    target = list_runs(session, dataset_id=dataset_id)[0]
    target.status = "running"
    target.summary_json = None
    target.finished_at = None
    session.commit()

    compare_latest(session, dataset_id=dataset_id, settings=SETTINGS)

    session.expire_all()
    reloaded = session.get(type(target), target.id)
    assert reloaded is not None
    assert reloaded.status == "running"
    assert reloaded.summary_json is None
    assert reloaded.finished_at is None
