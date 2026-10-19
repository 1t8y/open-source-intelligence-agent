"""The runner: execution, persistence, aggregation, and the promotion gate."""

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

#: The full five-target sweep is expensive enough to warrant module scope reuse; every test
#: below builds its own database, so the constant only avoids re-deriving the list.
ALL_TARGETS = TARGET_ORDER


def _seeded(session: Session, workspace: Workspace) -> int:
    """Seed the world and import the shipped dataset, returning its id."""
    seed_eval_world(session, workspace)
    return import_shipped_dataset(session)


def test_the_target_order_is_baseline_then_agent_then_ablations() -> None:
    """The report reads baseline first, then the full Agent, then the ablations."""
    assert ORDER == (
        "baseline_single_prompt",
        "agent_full",
        "agent_no_verifier",
        "agent_no_tools",
        "agent_no_history",
    )
    assert ALL_TARGETS == EVAL_TARGET_TYPES


def test_resolve_targets_defaults_to_every_variant() -> None:
    """Omitting the selection must run everything, not nothing."""
    assert resolve_targets(None) == TARGET_ORDER
    assert resolve_targets([]) == TARGET_ORDER


def test_resolve_targets_keeps_report_order_not_request_order() -> None:
    """A caller may ask in any order; the report is always comparable."""
    assert resolve_targets(["agent_no_tools", "baseline_single_prompt"]) == (
        "baseline_single_prompt",
        "agent_no_tools",
    )


def test_resolve_targets_refuses_an_unknown_variant() -> None:
    """A typo must not silently run a subset."""
    with pytest.raises(UnknownTarget) as excinfo:
        resolve_targets(["agent_full", "agent_oops"])
    assert "agent_oops" in str(excinfo.value)


def test_a_full_sweep_produces_the_unified_report(session: Session, workspace: Workspace) -> None:
    """The headline requirement: every variant runs and lands in one comparable report."""
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
    # The baseline gets one model call and no tools, so it has nothing to average.
    assert baseline.tool_call_total == 0
    assert baseline.verification_sufficient_rate is None

    full = report.targets[1]
    assert full.metrics["citation_coverage"] == Decimal("1.0000")
    assert full.metrics["task_completion"] == Decimal("1.0000")
    assert full.metrics["factual_consistency"] == Decimal("1.0000")
    # Not exactly 1.0: the sparse topic has no cached README, so one content read legitimately
    # fails. A tool success rate below the `docs/evaluation.md` §3 goal is a finding, not a bug,
    # and the report has to be able to show it.
    assert full.metrics["tool_success_rate"] is not None
    assert full.metrics["tool_success_rate"] > Decimal("0.90")
    assert full.metrics["source_accuracy"] is not None
    assert full.tool_call_total > 0


def test_the_agent_improves_citation_coverage_over_the_baseline(
    session: Session, workspace: Workspace
) -> None:
    """`docs/evaluation.md` §3 requires a measurable lift, and here it must be visible."""
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
    """§8 asks what a capability is worth, so the delta must be the loss, not the gain.

    Against the baseline the `agent_no_tools` delta would be a meaningless `0.0000`, because both
    variants cite nothing. Against the Agent it is the `-1.0000` the ablation exists to show.
    """
    dataset_id = _seeded(session, workspace)
    report = run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    ablated = next(item for item in report.targets if item.target_type == "agent_no_tools")
    assert ablated.reference_target == "agent_full"
    assert ablated.metrics["citation_coverage"] == Decimal("0.0000")
    assert ablated.deltas_vs_reference["citation_coverage"] == Decimal("-1.0000")
    assert ablated.deltas_vs_reference["factual_consistency"] == Decimal("-1.0000")
    assert ablated.regression is True
    # The ablation regressing must not veto the promotion verdict for the Agent itself.
    assert report.passed is True
    assert "agent_no_tools" in report.to_dict()["regressions"]


def test_no_tools_reads_nothing(session: Session, workspace: Workspace) -> None:
    """The ablation must be identifiable in the report, not just in a failing metric."""
    dataset_id = _seeded(session, workspace)
    report = run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    ablated = next(item for item in report.targets if item.target_type == "agent_no_tools")
    full = next(item for item in report.targets if item.target_type == "agent_full")
    # Only the six chapter writes remain, so the tool activity is strictly smaller than the
    # full Agent's research traffic.
    assert ablated.tool_call_total < full.tool_call_total
    assert ablated.metrics["source_accuracy"] is None  # no citation was ever claimed
    assert ablated.prompt_injection_block_rate is None  # nothing was read, so no injection surface


def test_no_history_loses_the_prior_report_baseline(session: Session, workspace: Workspace) -> None:
    """The ablation that drops history must not be indistinguishable from the full Agent."""
    dataset_id = _seeded(session, workspace)
    report = run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    ablated = next(item for item in report.targets if item.target_type == "agent_no_history")
    full = next(item for item in report.targets if item.target_type == "agent_full")
    assert ablated.metrics["citation_coverage"] is not None
    assert full.metrics["citation_coverage"] is not None
    assert ablated.metrics["citation_coverage"] <= full.metrics["citation_coverage"]
    # The damage is concrete: the prior-report call is denied on every case, and without a
    # baseline to compare against the coverage gate can never be satisfied.
    assert ablated.tool_call_denied == ablated.case_count
    assert ablated.verification_sufficient_rate == Decimal("0.0000")
    assert full.tool_call_denied == 0
    assert full.verification_sufficient_rate is not None
    assert full.verification_sufficient_rate > Decimal("0.0")


def test_no_verifier_keeps_the_draft_but_drops_the_gate(
    session: Session, workspace: Workspace
) -> None:
    """Removing the verifier must not zero out the draft; it removes a gate, not a capability."""
    dataset_id = _seeded(session, workspace)
    report = run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    ablated = next(item for item in report.targets if item.target_type == "agent_no_verifier")
    assert ablated.metrics["task_completion"] == Decimal("1.0000")  # all six sections drafted
    assert ablated.metrics["citation_coverage"] == Decimal("0.0000")
    assert ablated.reference_target == "agent_full"
    assert ablated.regression is True
    # The verification still ran and still reported a verdict - it just always says "sufficient",
    # which is exactly the gate whose contribution this ablation measures.
    assert ablated.verification_sufficient_rate == Decimal("1.0000")
    full = next(item for item in report.targets if item.target_type == "agent_full")
    assert full.verification_sufficient_rate is not None
    assert full.verification_sufficient_rate < Decimal("1.0000")


def test_every_run_row_carries_its_own_summary(session: Session, workspace: Workspace) -> None:
    """One row per variant must answer "what did this variant conclude" on its own."""
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
    """`UNIQUE(eval_run_id, eval_case_id)` is what makes a retry idempotent."""
    dataset_id = _seeded(session, workspace)
    run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    for row in list_runs(session, dataset_id=dataset_id):
        results = session.scalars(select(EvalResult).where(EvalResult.eval_run_id == row.id)).all()
        assert len(results) == 3
        assert len({item.eval_case_id for item in results}) == 3


def test_rerunning_a_target_updates_rows_instead_of_appending(
    session: Session, workspace: Workspace
) -> None:
    """A second sweep must keep the result table at one row per case per run."""
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
    assert len(latest) == 1  # one variant, so the newest run of that variant wins
    assert latest[0].id != first_runs[0].id  # a new run row, not an updated one
    assert len(list_runs(session, dataset_id=dataset_id)) == 2
    for row in list_runs(session, dataset_id=dataset_id):
        assert (
            len(session.scalars(select(EvalResult).where(EvalResult.eval_run_id == row.id)).all())
            == 3
        )


def test_compare_latest_reproduces_the_report_without_rerunning(
    session: Session, workspace: Workspace
) -> None:
    """The stored summary must be enough to rebuild the comparison."""
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
    """Comparing nothing must be an explicit error, not an empty report."""
    dataset_id = _seeded(session, workspace)
    with pytest.raises(EvaluationNotFound):
        compare_latest(session, dataset_id=dataset_id, settings=SETTINGS)


def test_aggregate_run_skips_not_applicable_metrics(session: Session, workspace: Workspace) -> None:
    """A mean over a metric the variant never exercised must stay None, not become zero."""
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
    """A broken case must be visible as a failure instead of silently scoring zero.

    The dataset is valid when imported and its topic is switched off afterwards, because since
    T25 a dataset naming a topic nobody has is refused at import. "Topic vanished between import
    and run" is the shape this defence can actually meet in production.
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
    """Running an empty selection would produce a meaningless report."""
    seed_eval_world(session, workspace)
    document = build_dataset_document(
        cases=[build_case_document(case_key="parked", enabled=False)],
    )
    dataset_id = import_dataset(session, parse_dataset(document)).dataset_id
    with pytest.raises(EvaluationNotFound):
        run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)


def test_evaluation_can_be_switched_off(session: Session, workspace: Workspace) -> None:
    """An operator must be able to disable the subsystem without touching code."""
    dataset_id = _seeded(session, workspace)
    disabled = build_settings(eval_enabled=False)
    with pytest.raises(EvaluationDisabled):
        run_evaluation(session, dataset_id=dataset_id, settings=disabled, now=WINDOW_END)


def test_the_regression_tolerance_is_configurable(session: Session, workspace: Workspace) -> None:
    """A loose tolerance must stop flagging the ablation, proving the gate reads the setting."""
    dataset_id = _seeded(session, workspace)
    loose = build_settings(eval_regression_tolerance=1.0)
    report = run_evaluation(session, dataset_id=dataset_id, settings=loose, now=WINDOW_END)
    ablated = next(item for item in report.targets if item.target_type == "agent_no_tools")
    assert ablated.regression is False


def test_list_runs_filters_by_target(session: Session, workspace: Workspace) -> None:
    """The run list must be addressable by variant."""
    dataset_id = _seeded(session, workspace)
    run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    rows = list_runs(session, dataset_id=dataset_id, target_type="agent_full")
    assert [row.target_type for row in rows] == ["agent_full"]


def test_latest_runs_per_target_returns_the_newest_of_each(
    session: Session, workspace: Workspace
) -> None:
    """The comparison reads one row per variant, and the newest one wins."""
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
    """The gate must cover exactly the metrics `docs/evaluation.md` §8 names."""
    assert GATED_METRICS == ("citation_coverage", "factual_consistency", "task_completion")


def test_only_the_agent_decides_promotion() -> None:
    """A control cannot veto a release; the candidate and the controls are different roles."""
    assert PROMOTION_TARGETS == ("agent_full",)
    assert BASELINE_TARGET not in PROMOTION_TARGETS
    assert not any(name.startswith("agent_no") for name in PROMOTION_TARGETS)


def test_a_candidate_free_sweep_judges_every_variant_it_has(
    session: Session, workspace: Workspace
) -> None:
    """A baseline-only diagnostic run has no candidate, so it must not report a vacuous pass."""
    dataset_id = _seeded(session, workspace)
    report = run_evaluation(
        session,
        dataset_id=dataset_id,
        target_types=[BASELINE_TARGET],
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert [item.target_type for item in report.targets] == [BASELINE_TARGET]
    # The baseline cites nothing, so it misses the absolute targets and the sweep is not "passed".
    assert report.passed is False
    assert report.to_dict()["failed_gates"][BASELINE_TARGET]


def test_the_report_serializes_to_json_safe_values(session: Session, workspace: Workspace) -> None:
    """The payload has to survive `json.dumps`, since the API returns it verbatim."""
    import json

    dataset_id = _seeded(session, workspace)
    report = run_evaluation(session, dataset_id=dataset_id, settings=SETTINGS, now=WINDOW_END)
    payload = report.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["dataset"]["version"] == "v1"
    assert set(payload["targets"][1]["metrics"]) >= set(GATED_METRICS)


def test_runs_and_cases_stay_addressable_by_dataset(session: Session, workspace: Workspace) -> None:
    """Two datasets must not mix their runs or cases."""
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
    """Import a dataset that is valid *now*, then switch its topic off.

    Since T25 the import refuses an enabled case whose topic this deployment does not have, so a
    dataset can no longer be born broken. The runner still has to defend against a topic that
    disappears between import and run (deleted, renamed, disabled), and this builds exactly that
    state instead of one the importer would reject.
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
    """A wholly failed sweep must still leave evidence of what was attempted."""
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
    """Provider exception text may contain credentials and must not be stored verbatim."""
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
    """No measurement is not a passing measurement.

    Every gate is a threshold on a score, so a variant whose cases all failed has no score, no
    failing threshold, and - without this rule - a clean promotion verdict. That is the most
    dangerous direction for a release gate to be wrong in.
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
