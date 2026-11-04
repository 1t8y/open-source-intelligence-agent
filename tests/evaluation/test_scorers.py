"""守护确定性的打分行为，包括每一种「不适用」的情形。

那些 `None` 结果和数字一样要紧：用例从未触达的指标必须被排除在汇总之外，
而不能当成零分计入，所以这些用例把两个方向都钉住了。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import count

from evaluation_support import (
    WINDOW_END,
    build_dataset_document,
    build_variant_output,
    seed_eval_world,
)
from sqlalchemy.orm import Session
from support import Workspace

from open_source_intelligence.agent.tools.contracts import REPORT_SECTION_KEYS, CitationRef
from open_source_intelligence.evaluation.dataset import import_dataset, list_cases, parse_dataset
from open_source_intelligence.evaluation.scorers import (
    score_case,
    score_citation_coverage,
    score_factual_consistency,
    score_failed_case,
    score_prompt_injection,
    score_source_accuracy,
    score_task_completion,
)
from open_source_intelligence.evaluation.targets import SectionDraft

SNAPSHOT_DATE = date(2026, 10, 11)

#: `(name, version)` 有唯一约束，所以每次调用辅助函数都要用新的数据集名。
_DATASET_SEQUENCE = count(1)


def _metric_citation(repository_id: int, when: date = SNAPSHOT_DATE) -> CitationRef:
    """返回一条指向某个仓库快照的指标引用。"""
    return CitationRef(source_type="metric", source_ref=f"repo_snapshots:{repository_id}:{when}")


def _section(
    key: str,
    text: str,
    *citations: CitationRef,
) -> SectionDraft:
    """构造一个章节草稿。"""
    return SectionDraft(section_key=key, content_markdown=text, citations=tuple(citations))


def _source_id(session: Session) -> int:
    """返回种子数据里那个 GitHub 数据源的 id。"""
    from open_source_intelligence.persistence.models.intelligence import DataSource

    source = session.query(DataSource).order_by(DataSource.id).first()
    assert source is not None
    return int(source.id)


def _repository(session: Session, full_name: str):
    """按全名返回一个种子仓库。"""
    from open_source_intelligence.persistence.models.intelligence import Repository

    return session.query(Repository).filter(Repository.full_name == full_name).one()


def test_citation_coverage_counts_numeric_sections_only(
    session: Session, workspace: Workspace
) -> None:
    """证明做了数字断言的章节，背后必须有可解析的指标来源。"""
    seed_eval_world(session, workspace)
    repository = workspace.add_repository(
        source_id=_source_id(session), provider_repo_id="cov", full_name="a/cov"
    )
    workspace.add_snapshot(repository.id, SNAPSHOT_DATE, stars=10, forks=1)
    sections = (
        _section("summary", "净增 40 stars。", _metric_citation(repository.id)),
        _section("trend", "净增 12 stars。"),  # 有数字断言，却没有引用
        _section("risks", "本期未发现风险。"),  # 没有数字，不算数字断言
    )
    assert score_citation_coverage(session, sections) == Decimal("0.5000")


def test_citation_coverage_is_not_applicable_without_numeric_claims(
    session: Session, workspace: Workspace
) -> None:
    """证明没有数字的报告无覆盖可谈，指标是 None 而不是 1。"""
    seed_eval_world(session, workspace)
    sections = (_section("summary", "本期没有可用观测。"),)
    assert score_citation_coverage(session, sections) is None
    assert score_citation_coverage(session, ()) is None


def test_a_dangling_citation_does_not_count_as_coverage(
    session: Session, workspace: Workspace
) -> None:
    """证明指向不存在快照的引用不能被算作证据。"""
    seed_eval_world(session, workspace)
    sections = (_section("summary", "净增 40 stars。", _metric_citation(999_999)),)
    assert score_citation_coverage(session, sections) == Decimal("0.0000")


def test_source_accuracy_separates_resolvable_from_dangling(
    session: Session, workspace: Workspace
) -> None:
    """这些引用里有一半指向空处，所以准确率必须是一半。"""
    seed_eval_world(session, workspace)
    repository = workspace.add_repository(
        source_id=_source_id(session), provider_repo_id="acc", full_name="a/acc"
    )
    workspace.add_snapshot(repository.id, SNAPSHOT_DATE, stars=10, forks=1)
    sections = (
        _section("summary", "净增 40 stars。", _metric_citation(repository.id)),
        _section("trend", "净增 12 stars。", _metric_citation(999_999)),
    )
    assert score_source_accuracy(session, sections) == Decimal("0.5000")


def test_source_accuracy_is_not_applicable_without_citations(
    session: Session, workspace: Workspace
) -> None:
    """没有引用，说明这个指标从未被触达。"""
    seed_eval_world(session, workspace)
    assert score_source_accuracy(session, (_section("summary", "无引用。"),)) is None


def test_task_completion_measures_present_required_sections() -> None:
    """只有 `required_sections` 点名的章节才计入完成度。"""
    sections = (
        _section("summary", "ok"),
        _section("trend", "ok"),
        _section("risks", "   "),  # 章节在，但内容是空的，所以不算
    )
    assert score_task_completion(sections) == Decimal("0.3333")
    assert score_task_completion(sections, required_sections=("summary", "trend")) == Decimal(
        "1.0000"
    )
    assert score_task_completion(sections, required_sections=("summary",)) == Decimal("1.0000")
    assert score_task_completion(sections, required_sections=()) is None


def test_every_template_section_is_required_by_default() -> None:
    """默认要求就是报告模板，所以半成品草稿没法看起来完整。"""
    complete = tuple(_section(key, "ok") for key in REPORT_SECTION_KEYS)
    assert score_task_completion(complete) == Decimal("1.0000")
    assert len(REPORT_SECTION_KEYS) == 6


def test_factual_consistency_requires_a_citation_and_a_matching_value(
    session: Session, workspace: Workspace
) -> None:
    """数字对却没来源只是猜的；有来源但数字错就是错。"""
    seed_eval_world(session, workspace)
    repository = _repository(session, "acme/rocket")
    case = _stored_case(session, facts=[{"repo": "acme/rocket", "field": "stars", "value": 1240}])

    cited_correct = (_section("summary", "1240 stars", _metric_citation(repository.id)),)
    assert score_factual_consistency(session, case=case, sections=cited_correct) == Decimal(
        "1.0000"
    )

    uncited = (_section("summary", "1240 stars"),)
    assert score_factual_consistency(session, case=case, sections=uncited) == Decimal("0.0000")

    wrong_value = _stored_case(
        session, facts=[{"repo": "acme/rocket", "field": "stars", "value": 7}]
    )
    assert score_factual_consistency(session, case=wrong_value, sections=cited_correct) == Decimal(
        "0.0000"
    )


def test_factual_consistency_accepts_a_repository_id(
    session: Session, workspace: Workspace
) -> None:
    """代码里构造的用例用 id 指向仓库，这套指法必须解析出同样的结果。"""
    seed_eval_world(session, workspace)
    repository = _repository(session, "acme/orbit")
    case = _stored_case(session, facts=[{"repo_id": repository.id, "field": "forks", "value": 96}])
    sections = (_section("summary", "96 forks", _metric_citation(repository.id)),)
    assert score_factual_consistency(session, case=case, sections=sections) == Decimal("1.0000")


def test_factual_consistency_is_not_applicable_without_facts(
    session: Session, workspace: Workspace
) -> None:
    """没有参考答案的用例，没有什么可核对。"""
    seed_eval_world(session, workspace)
    case = _stored_case(session, facts=None)
    assert score_factual_consistency(session, case=case, sections=()) is None


def test_an_unsupported_fact_field_is_ignored(session: Session, workspace: Workspace) -> None:
    """schema 核不了的事实属于不适用，而不是算错。

    这个指标要回答的是「在可核对的事实里，有多少条对上了」。
    数据集条目点了一个本 schema 不跟踪的字段，说明不了报告的任何问题，
    把它塞进分母，就会把一份正确的报告报成质量回退。
    """
    seed_eval_world(session, workspace)
    repository = _repository(session, "acme/rocket")
    case = _stored_case(session, facts=[{"repo": "acme/rocket", "field": "watts", "value": 1}])
    sections = (_section("summary", "1", _metric_citation(repository.id)),)
    assert score_factual_consistency(session, case=case, sections=sections) is None


def test_an_unknown_repository_is_not_a_miss(session: Session, workspace: Workspace) -> None:
    """关于一个已不存在仓库的事实无法核对，所以不算算错。"""
    seed_eval_world(session, workspace)
    repository = _repository(session, "acme/rocket")
    case = _stored_case(
        session,
        facts=[
            {"repo": "acme/rocket", "field": "stars", "value": 999},
            {"repo": "gone/missing", "field": "stars", "value": 1},
        ],
    )
    sections = (_section("summary", "1", _metric_citation(repository.id)),)
    # 一条可核对的事实，而报告与它不符：是 1 条里错 0 条，不是 2 条里错 0 条。
    assert score_factual_consistency(session, case=case, sections=sections) == Decimal("0.0000")


def test_injection_scoring_needs_a_read_surface() -> None:
    """没读过任何不可信内容的目标属于不适用，而不是自动判定安全。"""
    silent = build_variant_output(case_key="c", target_type="baseline_single_prompt")
    assert score_prompt_injection(silent, injection_expected=True) is None

    writing_only = build_variant_output(
        case_key="c",
        target_type="agent_no_tools",
        tool_call_total=6,
        tool_call_read=0,
    )
    assert score_prompt_injection(writing_only, injection_expected=True) is None


def test_injection_scoring_asserts_both_directions() -> None:
    """探针必须触发守卫，干净用例则不能触发。"""
    caught = build_variant_output(
        case_key="c",
        target_type="agent_full",
        tool_call_total=9,
        tool_call_read=4,
        guard_triggered=True,
    )
    assert score_prompt_injection(caught, injection_expected=True) is True
    assert score_prompt_injection(caught, injection_expected=False) is False

    missed = build_variant_output(
        case_key="c",
        target_type="agent_full",
        tool_call_total=9,
        tool_call_read=4,
        guard_triggered=False,
    )
    assert score_prompt_injection(missed, injection_expected=True) is False
    assert score_prompt_injection(missed, injection_expected=False) is True


def test_score_case_collects_details_and_keeps_not_applicable_as_none(
    session: Session, workspace: Workspace
) -> None:
    """一行评分既要带上受闸门约束的比率，也要带上支撑细节。"""
    seed_eval_world(session, workspace)
    repository = _repository(session, "acme/rocket")
    case = _stored_case(
        session,
        facts=[{"repo": "acme/rocket", "field": "stars", "value": 1240}],
        tags=["growth"],
    )
    output = build_variant_output(
        case_key=case.case_key,
        target_type="agent_full",
        sections=(
            _section("summary", "1240 stars", _metric_citation(repository.id)),
            _section("trend", "净增 40", _metric_citation(repository.id)),
        ),
        tool_call_total=5,
        tool_call_succeeded=5,
        tool_call_read=3,
        prompt_tokens=100,
        completion_tokens=50,
        cost=Decimal("0.25"),
        latency_ms=120,
        verification_sufficient=True,
    )
    scored = score_case(session, case=case, output=output)
    assert scored.citation_coverage == Decimal("1.0000")
    assert scored.factual_consistency == Decimal("1.0000")
    assert scored.task_completion == Decimal("0.3333")
    assert scored.source_accuracy == Decimal("1.0000")
    assert scored.tool_success_rate == Decimal("1.0000")
    assert scored.prompt_injection_blocked is True
    payload = scored.to_dict()
    assert payload["total_tokens"] == 150
    assert payload["section_count"] == 2
    assert payload["citation_count"] == 2
    assert payload["status"] == "succeeded"


def test_a_failed_case_scores_nothing_rather_than_zero(session: Session) -> None:
    """一次根本没跑起来的运行，没有证明质量差。"""
    scored = score_failed_case(
        case_key="c",
        status="failed",
        error_code="EVAL_TOPIC_NOT_FOUND",
        error_message="missing",
    )
    assert scored.citation_coverage is None
    assert scored.factual_consistency is None
    assert scored.task_completion is None
    assert scored.source_accuracy is None
    assert scored.tool_success_rate is None
    assert scored.prompt_injection_blocked is None
    assert scored.cost == Decimal("0")
    assert scored.details["error_code"] == "EVAL_TOPIC_NOT_FOUND"


def _stored_case(session: Session, *, facts, tags=None):
    """导入一份只含单用例的数据集，带上给定的参考事实，并返回该用例行。

    每次调用都用新的数据集名导入：产品里 `(name, version)` 不可变，
    而这些用例刻意在同一个库里构造好几个不同的用例。
    """
    case_document = {
        "case_key": "scored",
        "topic": "AI Agent",
        "window": {"start": "2026-10-05T00:00:00+00:00", "end": WINDOW_END.isoformat()},
        "tags": tags or [],
        "reference": {"facts": facts} if facts is not None else {"summary": "no facts"},
        "rubric": {},
        "enabled": True,
    }
    document = build_dataset_document(
        name=f"scorer-cases-{next(_DATASET_SEQUENCE)}",
        cases=[case_document],
    )
    dataset_id = import_dataset(session, parse_dataset(document)).dataset_id
    return next(
        row for row in list_cases(session, dataset_id=dataset_id) if row.case_key == "scored"
    )


def test_a_timezone_aware_window_is_preserved(session: Session, workspace: Workspace) -> None:
    """证明落库的窗口保留偏移量，因为打分比较的是绝对时刻。"""
    seed_eval_world(session, workspace)
    case = _stored_case(session, facts=None)
    start = datetime.fromisoformat(case.input_json["window"]["start"])
    assert start.tzinfo is not None
    assert start.astimezone(UTC).isoformat() == "2026-10-05T00:00:00+00:00"


def test_a_full_name_citation_counts_as_citing_the_repository(
    session: Session, workspace: Workspace
) -> None:
    """证明以 `owner/name` 这种持久形式写下的引用必须被认得，而不是算成没引。

    生成的报告把引用锚在稳定的全名上，这样库重建之后引用依然有效。
    只会解析草稿期数字 id 的打分器，把这种写法读成「什么都没引」，
    进而判报告事实不一致 —— 惩罚的恰恰是同一条断言的更正确写法。
    """
    seed_eval_world(session, workspace)
    repository = _repository(session, "acme/rocket")
    case = _stored_case(
        session,
        facts=[{"repo": "acme/rocket", "field": "stars", "value": repository.stars}],
    )
    citation = CitationRef(
        source_type="metric",
        source_ref=f"repo_snapshots:acme/rocket:{SNAPSHOT_DATE.isoformat()}",
    )
    sections = (_section("summary", "1", citation),)

    assert score_factual_consistency(session, case=case, sections=sections) == Decimal("1.0000")


def test_a_fact_is_checked_against_the_case_window_not_the_newest_row(
    session: Session, workspace: Workspace
) -> None:
    """事实描述的是一个窗口，所以后来的快照不能把正确的报告变成错的。

    按 `ORDER BY snapshot_date DESC LIMIT 1` 取值，会让同一份报告先得 1.0、
    等采集器再跑一次就变成 0.0 —— 那正是这个用例要免疫的漂移。
    """
    seed_eval_world(session, workspace)
    repository = workspace.add_repository(
        source_id=_source_id(session), provider_repo_id="win", full_name="a/window"
    )
    workspace.add_snapshot(repository.id, SNAPSHOT_DATE, stars=10, forks=1)
    # 一条落在用例窗口之后的快照：报告写出来时它还根本不存在。
    workspace.add_snapshot(repository.id, date(2026, 10, 20), stars=999, forks=99)
    case = _stored_case(
        session,
        facts=[{"repo": "a/window", "field": "stars", "value": 10}],
    )
    sections = (_section("summary", "1", _metric_citation(repository.id)),)

    assert score_factual_consistency(session, case=case, sections=sections) == Decimal("1.0000")
