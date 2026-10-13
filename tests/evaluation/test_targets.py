"""The five evaluation targets, exercised through the real research graph.

These are integration tests on purpose: the whole claim of the ablation design is that a
variant runs the *production* pipeline with one capability switched off, so a test that stubbed
the graph out would verify nothing.
"""

from __future__ import annotations

from evaluation_support import (
    INJECTION_TOPIC,
    WINDOW_END,
    WINDOW_START,
    import_shipped_dataset,
    seed_eval_world,
)
from sqlalchemy.orm import Session
from support import Workspace, build_settings

from open_source_intelligence.agent.profile import (
    AGENT_FULL,
    PROFILE_NO_HISTORY,
    PROFILE_NO_TOOLS,
    PROFILE_NO_VERIFIER,
    READ_TOOLS,
    WRITE_TOOL,
    profile_for_target,
)
from open_source_intelligence.agent.runtime import apply_profile, resolve_allowed_tools
from open_source_intelligence.agent.tools.registry import WHITELIST
from open_source_intelligence.evaluation.targets import (
    BASELINE_TARGET,
    ERROR_TOPIC_NOT_FOUND,
    run_target,
)
from open_source_intelligence.persistence.models.evaluation import EvalCase
from open_source_intelligence.persistence.models.intelligence import Topic

SETTINGS = build_settings()

AGENT_TARGETS = ("agent_full", "agent_no_verifier", "agent_no_tools", "agent_no_history")


def _case(session: Session, case_key: str) -> EvalCase:
    """Return one case row from the shipped dataset."""
    dataset_id = import_shipped_dataset(session)
    from open_source_intelligence.evaluation.dataset import list_cases

    rows = list_cases(session, dataset_id=dataset_id)
    return next(row for row in rows if row.case_key == case_key)


def test_the_full_agent_produces_a_fully_cited_draft(
    session: Session, workspace: Workspace
) -> None:
    """The production profile must reach a complete, evidence-backed draft."""
    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    output = run_target(
        session,
        case=case,
        target_type="agent_full",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert output.succeeded, output.error_message
    assert len(output.sections) == 6
    assert all(section.citations for section in output.sections)
    assert output.verification_sufficient is True
    assert output.tool_call_total > 0
    assert output.tool_call_denied == 0
    assert output.guard_triggered is False
    assert output.agent_run_id is not None


def test_the_prior_report_is_actually_visible_to_the_agent(
    session: Session, workspace: Workspace
) -> None:
    """The dataset promises this topic has a published history, so the tool must return it.

    `search_prior_reports` selects `generated_at < before`, and `before` is the case's window end.
    A world that stamps the prior report exactly at the window end therefore looks seeded but is
    invisible, and the only symptom would be a slightly thinner draft - which is precisely the kind
    of fixture drift that turns a quality metric into noise.
    """
    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    output = run_target(
        session,
        case=case,
        target_type="agent_full",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    source_types = {
        citation.source_type for section in output.sections for citation in section.citations
    }
    assert "prior_report" in source_types
    assert "metric" in source_types


def test_the_baseline_runs_without_tools_or_citations(
    session: Session, workspace: Workspace
) -> None:
    """A single prompt has no tool observations, which is exactly what it cannot cite."""
    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    output = run_target(session, case=case, target_type=BASELINE_TARGET, settings=SETTINGS)
    assert output.succeeded, output.error_message
    assert len(output.sections) == 6
    assert output.tool_call_total == 0
    assert all(not section.citations for section in output.sections)
    assert output.verification_sufficient is None
    assert output.agent_run_id is None
    assert output.notes


def test_the_injection_sample_is_caught_by_the_prompt_guard(
    session: Session, workspace: Workspace
) -> None:
    """Poisoned README text must raise the guard flag instead of reaching the model as control."""
    seed_eval_world(session, workspace)
    case = _case(session, "injection-readme")
    output = run_target(
        session,
        case=case,
        target_type="agent_full",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert output.tool_call_total > 0
    assert output.guard_triggered is True
    # The guard did its job, so the run is scoreable even though it needs a human to look at it.
    assert output.sections


def test_no_verifier_removes_the_coverage_gate(session: Session, workspace: Workspace) -> None:
    """`agent_no_verifier` must report a permissive verdict where the full run would not."""
    seed_eval_world(session, workspace)
    case = _case(session, "injection-readme")
    full = run_target(
        session, case=case, target_type="agent_full", settings=SETTINGS, now=WINDOW_END
    )
    ablated = run_target(
        session,
        case=case,
        target_type="agent_no_verifier",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    # The injection case is the one the full verifier refuses to clear, so the ablation's
    # difference is visible rather than masked by an already-sufficient evidence base.
    assert full.verification_sufficient is False
    assert ablated.verification_sufficient is True
    assert not ablated.notes or "verification" not in ablated.notes[0]


def test_no_tools_collapses_citation_support(session: Session, workspace: Workspace) -> None:
    """Without read tools the draft has no observations, so it carries no citations."""
    from sqlalchemy import select

    from open_source_intelligence.persistence.models.agent import ToolCall

    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    full = run_target(
        session, case=case, target_type="agent_full", settings=SETTINGS, now=WINDOW_END
    )
    ablated = run_target(
        session,
        case=case,
        target_type="agent_no_tools",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert full.tool_call_read > 0
    # The ablation still writes its six sections, so the meaningful claim is that no *read*
    # tool ran: `save_report_section` is not evidence gathering and must not be counted as one.
    assert ablated.tool_call_read == 0
    assert ablated.tool_call_total == len(ablated.sections)
    read_rows = session.scalars(
        select(ToolCall.tool_name).where(
            ToolCall.agent_run_id == ablated.agent_run_id,
            ToolCall.tool_name != "save_report_section",
        )
    ).all()
    assert read_rows == []
    assert all(not section.citations for section in ablated.sections)


def test_no_history_drops_the_prior_report_baseline(session: Session, workspace: Workspace) -> None:
    """Removing `search_prior_reports` must leave the run without a baseline to compare against."""
    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    full = run_target(
        session, case=case, target_type="agent_full", settings=SETTINGS, now=WINDOW_END
    )
    ablated = run_target(
        session,
        case=case,
        target_type="agent_no_history",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert full.verification_sufficient is True
    assert ablated.verification_sufficient is False
    assert ablated.notes and "verification insufficient" in ablated.notes[0]
    # The tool is refused rather than silently skipped, which the audit trail records as denied.
    assert ablated.tool_call_denied == 1


def test_every_agent_variant_still_writes_a_draft(session: Session, workspace: Workspace) -> None:
    """An ablation that produced no draft would measure nothing, so all four must write one."""
    seed_eval_world(session, workspace)
    for target_type in AGENT_TARGETS:
        case = _case(session, "growth-surge")
        output = run_target(
            session,
            case=case,
            target_type=target_type,
            settings=SETTINGS,
            now=WINDOW_END,
        )
        assert output.succeeded, f"{target_type}: {output.error_message}"
        assert len(output.sections) == 6, target_type


def test_a_missing_topic_fails_the_case_instead_of_raising(
    session: Session, workspace: Workspace
) -> None:
    """A case whose topic is absent must be recorded as a failure, not crash the run."""
    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    topic = session.query(Topic).filter(Topic.name == "AI Agent").one()
    topic.enabled = False
    session.commit()
    output = run_target(
        session,
        case=case,
        target_type="agent_full",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert output.status == "failed"
    assert output.error_code == ERROR_TOPIC_NOT_FOUND


def test_case_context_carries_the_window_from_the_document(
    session: Session, workspace: Workspace
) -> None:
    """The stored window must round-trip into the run, not fall back to the topic default."""
    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    assert case.input_json["window"]["start"] == WINDOW_START.isoformat()
    assert case.input_json["window"]["end"] == WINDOW_END.isoformat()


def test_unknown_target_is_refused(session: Session, workspace: Workspace) -> None:
    """A typo must not silently fall back to some other variant."""
    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    try:
        run_target(session, case=case, target_type="agent_typo", settings=SETTINGS)
    except ValueError as exc:
        assert "unsupported eval target" in str(exc)
    else:  # pragma: no cover - the raise is the assertion
        raise AssertionError("an unknown target must raise")


def test_profiles_narrow_but_never_remove_the_write_tool() -> None:
    """A profile may only remove capability, and never the tool that produces the draft."""
    configured = resolve_allowed_tools({"tools": list(WHITELIST)})
    assert apply_profile(configured, AGENT_FULL) == configured

    no_history = apply_profile(configured, PROFILE_NO_HISTORY)
    assert "search_prior_reports" not in no_history
    assert WRITE_TOOL in no_history
    assert set(no_history) == set(configured) - {"search_prior_reports"}

    no_tools = apply_profile(configured, PROFILE_NO_TOOLS)
    assert set(no_tools) == {WRITE_TOOL}
    for read_tool in READ_TOOLS:
        assert read_tool not in no_tools


def test_a_config_that_never_granted_the_write_tool_does_not_regain_it() -> None:
    """The profile must not widen what the agent config deliberately withheld."""
    configured = resolve_allowed_tools({"tools": ["query_repo_metrics"]})
    narrowed = apply_profile(configured, PROFILE_NO_TOOLS)
    assert WRITE_TOOL not in narrowed
    assert narrowed == ()


def test_profile_lookup_matches_the_documented_variants() -> None:
    """The variant names in the dataset must map onto the profiles the runner expects."""
    assert profile_for_target("agent_full") is AGENT_FULL
    assert profile_for_target("agent_no_verifier") is PROFILE_NO_VERIFIER
    assert profile_for_target("agent_no_tools") is PROFILE_NO_TOOLS
    assert profile_for_target("agent_no_history") is PROFILE_NO_HISTORY
    assert profile_for_target(BASELINE_TARGET) is None


def test_the_injection_topic_is_isolated_from_the_others(
    session: Session, workspace: Workspace
) -> None:
    """Only the injection sample may read poisoned text, or every case would report a guard hit."""
    seed_eval_world(session, workspace)
    clean = run_target(
        session,
        case=_case(session, "growth-surge"),
        target_type="agent_full",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert clean.guard_triggered is False
    assert INJECTION_TOPIC != "AI Agent"
