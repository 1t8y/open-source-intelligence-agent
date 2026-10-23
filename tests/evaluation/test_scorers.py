"""Deterministic scorer behaviour, including every "not applicable" case.

The `None` results matter as much as the numbers: a metric that a case never exercised must be
excluded from the aggregate rather than counted as a zero, so these tests pin both directions.
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

#: `(name, version)` is unique, so each helper call needs its own dataset name.
_DATASET_SEQUENCE = count(1)


def _metric_citation(repository_id: int, when: date = SNAPSHOT_DATE) -> CitationRef:
    """Return a metric citation pointing at one repository's snapshot."""
    return CitationRef(source_type="metric", source_ref=f"repo_snapshots:{repository_id}:{when}")


def _section(
    key: str,
    text: str,
    *citations: CitationRef,
) -> SectionDraft:
    """Build one draft section."""
    return SectionDraft(section_key=key, content_markdown=text, citations=tuple(citations))


def _source_id(session: Session) -> int:
    """Return the seeded GitHub source id."""
    from open_source_intelligence.persistence.models.intelligence import DataSource

    source = session.query(DataSource).order_by(DataSource.id).first()
    assert source is not None
    return int(source.id)


def _repository(session: Session, full_name: str):
    """Return one seeded repository by full name."""
    from open_source_intelligence.persistence.models.intelligence import Repository

    return session.query(Repository).filter(Repository.full_name == full_name).one()


def test_citation_coverage_counts_numeric_sections_only(
    session: Session, workspace: Workspace
) -> None:
    """A section that makes a numeric claim needs a resolvable metric source behind it."""
    seed_eval_world(session, workspace)
    repository = workspace.add_repository(
        source_id=_source_id(session), provider_repo_id="cov", full_name="a/cov"
    )
    workspace.add_snapshot(repository.id, SNAPSHOT_DATE, stars=10, forks=1)
    sections = (
        _section("summary", "净增 40 stars。", _metric_citation(repository.id)),
        _section("trend", "净增 12 stars。"),  # numeric claim, no citation
        _section("risks", "本期未发现风险。"),  # no digits, not a numeric claim
    )
    assert score_citation_coverage(session, sections) == Decimal("0.5000")


def test_citation_coverage_is_not_applicable_without_numeric_claims(
    session: Session, workspace: Workspace
) -> None:
    """A report with no numbers has nothing to cover, so the metric is None rather than 1."""
    seed_eval_world(session, workspace)
    sections = (_section("summary", "本期没有可用观测。"),)
    assert score_citation_coverage(session, sections) is None
    assert score_citation_coverage(session, ()) is None


def test_a_dangling_citation_does_not_count_as_coverage(
    session: Session, workspace: Workspace
) -> None:
    """A citation to a non-existent snapshot must not be credited as evidence."""
    seed_eval_world(session, workspace)
    sections = (_section("summary", "净增 40 stars。", _metric_citation(999_999)),)
    assert score_citation_coverage(session, sections) == Decimal("0.0000")


def test_source_accuracy_separates_resolvable_from_dangling(
    session: Session, workspace: Workspace
) -> None:
    """Half of these citations point at nothing, so accuracy must be one half."""
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
    """No citations means the metric was never exercised."""
    seed_eval_world(session, workspace)
    assert score_source_accuracy(session, (_section("summary", "无引用。"),)) is None


def test_task_completion_measures_present_required_sections() -> None:
    """Only the sections named in `required_sections` count toward completion."""
    sections = (
        _section("summary", "ok"),
        _section("trend", "ok"),
        _section("risks", "   "),  # present but empty, so it does not count
    )
    assert score_task_completion(sections) == Decimal("0.3333")
    assert score_task_completion(sections, required_sections=("summary", "trend")) == Decimal(
        "1.0000"
    )
    assert score_task_completion(sections, required_sections=("summary",)) == Decimal("1.0000")
    assert score_task_completion(sections, required_sections=()) is None


def test_every_template_section_is_required_by_default() -> None:
    """The default requirement is the report template, so a partial draft cannot look complete."""
    complete = tuple(_section(key, "ok") for key in REPORT_SECTION_KEYS)
    assert score_task_completion(complete) == Decimal("1.0000")
    assert len(REPORT_SECTION_KEYS) == 6


def test_factual_consistency_requires_a_citation_and_a_matching_value(
    session: Session, workspace: Workspace
) -> None:
    """A right number with no source is a guess, and a cited wrong number is an error."""
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
    """Cases built in code address a repository by id, which must resolve the same way."""
    seed_eval_world(session, workspace)
    repository = _repository(session, "acme/orbit")
    case = _stored_case(session, facts=[{"repo_id": repository.id, "field": "forks", "value": 96}])
    sections = (_section("summary", "96 forks", _metric_citation(repository.id)),)
    assert score_factual_consistency(session, case=case, sections=sections) == Decimal("1.0000")


def test_factual_consistency_is_not_applicable_without_facts(
    session: Session, workspace: Workspace
) -> None:
    """A case with no reference answer has nothing to check."""
    seed_eval_world(session, workspace)
    case = _stored_case(session, facts=None)
    assert score_factual_consistency(session, case=case, sections=()) is None


def test_an_unsupported_fact_field_is_ignored(session: Session, workspace: Workspace) -> None:
    """A fact the schema cannot check is not applicable, not a miss.

    The metric has to answer "of the facts that could be checked, how many agree". A dataset entry
    naming a field this schema does not track says nothing about the report, so folding it into
    the denominator would report a correct report as a quality regression.
    """
    seed_eval_world(session, workspace)
    repository = _repository(session, "acme/rocket")
    case = _stored_case(session, facts=[{"repo": "acme/rocket", "field": "watts", "value": 1}])
    sections = (_section("summary", "1", _metric_citation(repository.id)),)
    assert score_factual_consistency(session, case=case, sections=sections) is None


def test_an_unknown_repository_is_not_a_miss(session: Session, workspace: Workspace) -> None:
    """A fact about a repository that no longer exists is unverifiable, so it is not a miss."""
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
    # One checkable fact, and the report disagrees with it: 0 of 1, not 0 of 2.
    assert score_factual_consistency(session, case=case, sections=sections) == Decimal("0.0000")


def test_injection_scoring_needs_a_read_surface() -> None:
    """A target that read no untrusted content is not applicable, not automatically safe."""
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
    """A probe must trip the guard, and a clean case must not."""
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
    """One scored row must carry both the gated rates and the supporting detail."""
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
    """A run that never started has not demonstrated bad quality."""
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
    """Import a one-case dataset with the given reference facts and return the case row.

    Each call imports under a fresh dataset name: `(name, version)` is immutable in the product,
    and these tests deliberately build several different cases inside one database.
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
    """A stored window must keep its offset, since scoring compares instants."""
    seed_eval_world(session, workspace)
    case = _stored_case(session, facts=None)
    start = datetime.fromisoformat(case.input_json["window"]["start"])
    assert start.tzinfo is not None
    assert start.astimezone(UTC).isoformat() == "2026-10-05T00:00:00+00:00"


def test_a_full_name_citation_counts_as_citing_the_repository(
    session: Session, workspace: Workspace
) -> None:
    """A reference in the durable `owner/name` form has to be understood, not scored as absent.

    Generated reports anchor citations on the stable full name so they survive a database rebuild.
    A scorer that only decoded the drafting-time numeric id read that as "cited nothing" and
    marked the report's facts inconsistent — punishing the more correct form of the same claim.
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
    """Facts describe a window, so a later snapshot must not turn a correct report into a wrong one.

    Reading `ORDER BY snapshot_date DESC LIMIT 1` made the same report score 1.0 and then 0.0 as
    soon as the collector ran again — the drift the case exists to be immune to.
    """
    seed_eval_world(session, workspace)
    repository = workspace.add_repository(
        source_id=_source_id(session), provider_repo_id="win", full_name="a/window"
    )
    workspace.add_snapshot(repository.id, SNAPSHOT_DATE, stars=10, forks=1)
    # A snapshot after the case window: the report was written before this existed.
    workspace.add_snapshot(repository.id, date(2026, 10, 20), stars=999, forks=99)
    case = _stored_case(
        session,
        facts=[{"repo": "a/window", "field": "stars", "value": 10}],
    )
    sections = (_section("summary", "1", _metric_citation(repository.id)),)

    assert score_factual_consistency(session, case=case, sections=sections) == Decimal("1.0000")
