"""End-to-end tests for the bounded research Agent runtime."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from agent_support import seed_topic
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from support import NOW, Workspace, build_settings

from open_source_intelligence.agent.errors import (
    AGENT_BUDGET_EXCEEDED,
    AGENT_MODEL_UNAVAILABLE,
    AGENT_NO_CANDIDATES,
)
from open_source_intelligence.agent.llm.errors import MODEL_REQUEST_FAILED, ModelClientError
from open_source_intelligence.agent.llm.messages import ChatMessage, ModelTurn, ToolSchema
from open_source_intelligence.agent.runtime import DEFAULT_AGENT_CONFIG_NAME, run_research_agent
from open_source_intelligence.persistence.models.agent import (
    AgentConfig,
    AgentRun,
    AgentStep,
    ToolCall,
)
from open_source_intelligence.persistence.models.intelligence import (
    Repository,
    RepositoryContent,
    RepoSnapshot,
)
from open_source_intelligence.persistence.models.reporting import ReportDraft, ReportSection


class FailingClient:
    """Model client that always reports a transport failure."""

    @property
    def provider(self) -> str:
        return "failing"

    @property
    def model(self) -> str:
        return "failing-v1"

    def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema],
    ) -> ModelTurn:
        del messages, tools
        raise ModelClientError(MODEL_REQUEST_FAILED, "simulated transport failure")


def _count(session: Session, entity: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(entity)) or 0)


def test_runtime_completes_a_full_research_trail(session: Session, workspace: Workspace) -> None:
    seeded = seed_topic(workspace, repo_count=2)
    result = run_research_agent(
        session,
        topic_id=seeded.topic.id,
        settings=build_settings(),
        now=NOW,
    )

    assert result.status == "completed"
    assert result.error_code is None
    assert result.candidate_repo_ids == [repo.id for repo in seeded.repositories]
    assert result.section_count == 6
    assert result.report_draft_id is not None
    assert result.verification is not None
    assert result.verification.sufficient is True
    assert result.verification.covered_repo_ids == [repo.id for repo in seeded.repositories]

    agent_run = session.get(AgentRun, result.agent_run_id)
    assert agent_run is not None
    assert agent_run.status == "completed"
    assert agent_run.finished_at is not None
    assert agent_run.loop_count >= 3
    assert agent_run.model_call_count >= 4
    assert agent_run.tool_call_count >= 6

    steps = session.scalars(
        select(AgentStep)
        .where(AgentStep.agent_run_id == result.agent_run_id)
        .order_by(AgentStep.step_index)
    ).all()
    assert [step.step_index for step in steps] == list(range(1, len(steps) + 1))
    assert {step.step_type for step in steps} >= {"plan", "tool", "observe", "verify", "write"}
    assert all(step.summary for step in steps)

    drafts = session.scalars(select(ReportDraft)).all()
    assert len(drafts) == 1
    assert drafts[0].agent_run_id == result.agent_run_id

    sections = session.scalars(select(ReportSection)).all()
    assert len(sections) == 6
    assert {section.section_key for section in sections} == {
        "summary",
        "trend",
        "highlights",
        "risks",
        "recommendation",
        "appendix",
    }
    assert all(section.version == 1 for section in sections)
    assert all(section.citations_json for section in sections)


def test_runtime_records_every_tool_call_against_the_run(
    session: Session,
    workspace: Workspace,
) -> None:
    seeded = seed_topic(workspace, repo_count=1)
    result = run_research_agent(
        session,
        topic_id=seeded.topic.id,
        settings=build_settings(),
        now=NOW,
    )

    calls = session.scalars(
        select(ToolCall).where(ToolCall.agent_run_id == result.agent_run_id)
    ).all()
    assert calls
    assert {call.status for call in calls} == {"success"}
    names = {call.tool_name for call in calls}
    assert names == {
        "get_repository_profile",
        "query_repo_metrics",
        "search_prior_reports",
        "get_repository_content",
        "save_report_section",
    }
    assert sum(1 for call in calls if call.tool_name == "save_report_section") == 6


def test_runtime_marks_run_for_review_without_candidates(
    session: Session,
    workspace: Workspace,
) -> None:
    seeded = seed_topic(workspace, repo_count=0)
    result = run_research_agent(
        session,
        topic_id=seeded.topic.id,
        settings=build_settings(),
        now=NOW,
    )

    assert result.status == "needs_review"
    assert result.error_code == AGENT_NO_CANDIDATES
    assert result.section_count == 0
    assert result.report_draft_id is None
    assert int(session.scalar(select(func.count()).select_from(ReportSection)) or 0) == 0


def test_runtime_stops_when_the_model_budget_is_exhausted(
    session: Session,
    workspace: Workspace,
) -> None:
    seeded = seed_topic(workspace, repo_count=1)
    result = run_research_agent(
        session,
        topic_id=seeded.topic.id,
        settings=build_settings(agent_max_model_calls=1),
        now=NOW,
    )

    assert result.status == "needs_review"
    assert result.error_code == AGENT_BUDGET_EXCEEDED
    assert result.model_call_count == 1
    assert result.section_count == 0


def test_runtime_fails_when_the_model_is_unavailable(
    session: Session,
    workspace: Workspace,
) -> None:
    seeded = seed_topic(workspace, repo_count=1)
    result = run_research_agent(
        session,
        topic_id=seeded.topic.id,
        settings=build_settings(),
        client=FailingClient(),
        now=NOW,
    )

    assert result.status == "failed"
    assert result.error_code == AGENT_MODEL_UNAVAILABLE
    agent_run = session.get(AgentRun, result.agent_run_id)
    assert agent_run is not None
    assert agent_run.status == "failed"
    assert agent_run.error_code == AGENT_MODEL_UNAVAILABLE


def test_runtime_read_tools_leave_domain_tables_untouched(
    session: Session,
    workspace: Workspace,
) -> None:
    seeded = seed_topic(workspace, repo_count=2)
    before = (
        _count(session, Repository),
        _count(session, RepoSnapshot),
        _count(session, RepositoryContent),
    )

    run_research_agent(
        session,
        topic_id=seeded.topic.id,
        settings=build_settings(),
        now=NOW,
    )

    after = (
        _count(session, Repository),
        _count(session, RepoSnapshot),
        _count(session, RepositoryContent),
    )
    assert before == after


def test_runtime_honours_the_configured_tool_policy(
    session: Session,
    workspace: Workspace,
) -> None:
    seeded = seed_topic(workspace, repo_count=1)
    session.add(
        AgentConfig(
            name=DEFAULT_AGENT_CONFIG_NAME,
            version="v1",
            provider="offline",
            model="offline-deterministic-v1",
            prompt_version="v1",
            tool_policy_json={"tools": ["get_repository_profile"]},
            max_loops=4,
            max_model_calls=6,
            max_tool_calls=10,
            max_cost=Decimal("1"),
            enabled=True,
        )
    )
    session.commit()

    result = run_research_agent(
        session,
        topic_id=seeded.topic.id,
        settings=build_settings(),
        now=NOW,
    )

    calls = session.scalars(
        select(ToolCall).where(ToolCall.agent_run_id == result.agent_run_id)
    ).all()
    denied = [call for call in calls if call.status == "denied"]
    assert denied
    assert {call.tool_name for call in denied} == {
        "query_repo_metrics",
        "search_prior_reports",
        "get_repository_content",
    }
    assert all(call.error_code == "TOOL_ACCESS_DENIED" for call in denied)
    # The narrowed policy cannot satisfy the evidence check, so the run needs a human.
    assert result.status == "needs_review"
    assert result.section_count == 0
