"""Run T8 research Agent smoke checks against MySQL.

The script seeds a temporary topic with ranked candidates, a metric series, and cached
README content, then drives the real LangGraph runtime end to end on MySQL rather than
in-memory SQLite. It verifies the Research -> Tool -> Observe -> Verify -> Write trail,
step and tool-call recording, draft sections, read-only behaviour, and the two degraded
paths (no candidates, model unavailable).
"""

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.errors import AGENT_MODEL_UNAVAILABLE, AGENT_NO_CANDIDATES
from open_source_intelligence.agent.llm.errors import MODEL_REQUEST_FAILED, ModelClientError
from open_source_intelligence.agent.llm.messages import ChatMessage, ModelTurn, ToolSchema
from open_source_intelligence.agent.runtime import DEFAULT_AGENT_CONFIG_NAME, run_research_agent
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.models.agent import (
    AgentConfig,
    AgentRun,
    AgentStep,
    ToolCall,
)
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    PipelineRun,
    Repository,
    RepositoryContent,
    RepoSnapshot,
    Topic,
    TopicRepository,
)
from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportDraft,
    ReportSection,
    ReportSource,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)

VERIFIER_USERNAME = "__t8_verifier__"
TOPIC_NAME = "__T8 Research Topic__"
EMPTY_TOPIC_NAME = "__T8 Empty Topic__"
PROVIDER_PREFIX = "__t8_smoke_"
SMOKE_NOW = datetime(2099, 3, 5, 12, 0, tzinfo=UTC)
FIRST_SNAPSHOT_DATE = date(2099, 3, 1)
LAST_SNAPSHOT_DATE = date(2099, 3, 2)


class UnavailableClient:
    """Model client that always reports a transport failure."""

    @property
    def provider(self) -> str:
        return "unavailable"

    @property
    def model(self) -> str:
        return "unavailable-v1"

    def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema],
    ) -> ModelTurn:
        del messages, tools
        raise ModelClientError(MODEL_REQUEST_FAILED, "smoke transport failure")


def cleanup_verification_records(session: Session) -> None:
    """Remove every record created by a previous T8 smoke run."""
    topic_ids = list(
        session.scalars(
            select(Topic.id).where(Topic.name.in_((TOPIC_NAME, EMPTY_TOPIC_NAME)))
        ).all()
    )
    if topic_ids:
        run_ids = list(
            session.scalars(select(PipelineRun.id).where(PipelineRun.topic_id.in_(topic_ids))).all()
        )
        if run_ids:
            agent_run_ids = list(
                session.scalars(
                    select(AgentRun.id).where(AgentRun.pipeline_run_id.in_(run_ids))
                ).all()
            )
            if agent_run_ids:
                draft_ids = select(ReportDraft.id).where(
                    ReportDraft.agent_run_id.in_(agent_run_ids)
                )
                report_ids = select(Report.id).where(Report.agent_run_id.in_(agent_run_ids))
                session.execute(
                    delete(ReportSection).where(ReportSection.report_draft_id.in_(draft_ids))
                )
                session.execute(
                    delete(ReportDraft).where(ReportDraft.agent_run_id.in_(agent_run_ids))
                )
                session.execute(delete(ReportSource).where(ReportSource.report_id.in_(report_ids)))
                session.execute(delete(Report).where(Report.agent_run_id.in_(agent_run_ids)))
                session.execute(delete(ToolCall).where(ToolCall.agent_run_id.in_(agent_run_ids)))
                session.execute(delete(AgentStep).where(AgentStep.agent_run_id.in_(agent_run_ids)))
                session.execute(delete(AgentRun).where(AgentRun.id.in_(agent_run_ids)))
            session.execute(delete(PipelineRun).where(PipelineRun.id.in_(run_ids)))
        session.execute(delete(TopicRepository).where(TopicRepository.topic_id.in_(topic_ids)))
        session.execute(delete(Topic).where(Topic.id.in_(topic_ids)))
    session.execute(
        delete(Repository).where(Repository.provider_repo_id.like(f"{PROVIDER_PREFIX}%"))
    )
    # The runtime creates this config on first use; smoke runs are its only consumer here.
    session.execute(delete(AgentConfig).where(AgentConfig.name == DEFAULT_AGENT_CONFIG_NAME))
    session.execute(delete(User).where(User.username == VERIFIER_USERNAME))
    session.commit()


def parse_args() -> argparse.Namespace:
    """Parse the optional cleanup mode used by the verification script."""
    parser = argparse.ArgumentParser(description="Run T8 research Agent smoke checks")
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Only remove verification records and exit",
    )
    return parser.parse_args()


def _seed_source(session: Session) -> DataSource:
    """Return an existing data source or create a verification-only one."""
    source = session.scalar(select(DataSource).order_by(DataSource.id).limit(1))
    if source is None:
        source = DataSource(
            name="__t8_smoke_github__",
            source_type="github",
            base_url="https://api.github.com",
            enabled=True,
            rate_limit_per_minute=30,
        )
        session.add(source)
        session.flush()
    return source


def seed_topics(session: Session) -> dict[str, int]:
    """Create the research topic and an empty topic used by the degraded-path check."""
    user = User(
        username=VERIFIER_USERNAME,
        password_hash="t8-verification-only",
        display_name="T8 Verifier",
        status="active",
        data_scope="team",
    )
    session.add(user)
    session.flush()

    topic = Topic(
        name=TOPIC_NAME,
        min_stars=0,
        window_days=7,
        max_candidates=10,
        publish_mode="AUTO_WITH_DELAY",
        enabled=True,
        created_by=user.id,
    )
    empty_topic = Topic(
        name=EMPTY_TOPIC_NAME,
        min_stars=0,
        window_days=7,
        max_candidates=10,
        publish_mode="AUTO_WITH_DELAY",
        enabled=True,
        created_by=user.id,
    )
    session.add_all([topic, empty_topic])
    session.flush()

    source = _seed_source(session)
    repository_ids: list[int] = []
    for index in range(2):
        repository = Repository(
            source_id=source.id,
            provider_repo_id=f"{PROVIDER_PREFIX}repo_{index}",
            full_name=f"__t8_smoke__/repo-{index}",
            html_url=f"https://example.com/__t8_smoke__/repo-{index}",
            description="T8 smoke repository",
            language="Python",
            license="MIT",
            topics_json=["ai"],
            stars=100 + index,
            forks=10,
            first_seen_at=SMOKE_NOW,
            last_seen_at=SMOKE_NOW,
        )
        session.add(repository)
        session.flush()
        repository_ids.append(repository.id)
        session.add(
            TopicRepository(
                topic_id=topic.id,
                repository_id=repository.id,
                score=1,
                rank=index + 1,
                selected_for_agent=True,
            )
        )
        session.add_all(
            [
                RepoSnapshot(
                    repository_id=repository.id,
                    snapshot_date=FIRST_SNAPSHOT_DATE,
                    stars=100 + index,
                    forks=10,
                    stars_delta=5,
                    forks_delta=1,
                ),
                RepoSnapshot(
                    repository_id=repository.id,
                    snapshot_date=LAST_SNAPSHOT_DATE,
                    stars=140 + index,
                    forks=12,
                    stars_delta=40,
                    forks_delta=2,
                ),
            ]
        )
        session.add(
            RepositoryContent(
                repository_id=repository.id,
                content_type="readme",
                source_url=f"https://example.com/__t8_smoke__/repo-{index}/readme",
                content=f"# Smoke repo {index}\nA cached readme used by the T8 smoke check.",
                content_hash=f"__t8_smoke__{index}",
                fetched_at=SMOKE_NOW,
            )
        )
    session.commit()
    return {
        "topic_id": topic.id,
        "empty_topic_id": empty_topic.id,
        "repo_id": repository_ids[0],
    }


def _count(session: Session, entity: Any) -> int:
    """Return the row count of one table."""
    return int(session.scalar(select(func.count()).select_from(entity)) or 0)


def run_checks(session: Session, ids: dict[str, int]) -> dict[str, Any]:
    """Drive the full runtime on real MySQL and return the assertion payload."""
    settings = get_settings()
    topic_id = ids["topic_id"]
    empty_topic_id = ids["empty_topic_id"]

    snapshots_before = _count(session, RepoSnapshot)
    contents_before = _count(session, RepositoryContent)
    repositories_before = _count(session, Repository)

    result = run_research_agent(
        session,
        topic_id=topic_id,
        settings=settings,
        now=SMOKE_NOW,
    )
    second = run_research_agent(
        session,
        topic_id=topic_id,
        settings=settings,
        now=SMOKE_NOW,
    )

    agent_run = session.get(AgentRun, result.agent_run_id)
    if agent_run is None:
        raise RuntimeError("the Agent run row was not persisted")
    pipeline_run = session.get(PipelineRun, result.pipeline_run_id)
    if pipeline_run is None:
        raise RuntimeError("the pipeline run row was not persisted")

    steps = list(
        session.scalars(
            select(AgentStep)
            .where(AgentStep.agent_run_id == result.agent_run_id)
            .order_by(AgentStep.step_index)
        ).all()
    )
    calls = list(
        session.scalars(
            select(ToolCall)
            .where(ToolCall.agent_run_id == result.agent_run_id)
            .order_by(ToolCall.id)
        ).all()
    )
    sections = list(
        session.scalars(
            select(ReportSection).where(ReportSection.report_draft_id == result.report_draft_id)
        ).all()
    )

    if result.status != "completed":
        raise RuntimeError(f"expected a completed run, got {result.status}/{result.error_code}")
    if pipeline_run.status != "succeeded":
        raise RuntimeError(f"expected a succeeded pipeline run, got {pipeline_run.status}")
    if agent_run.status != "completed":
        raise RuntimeError(f"expected agent_run status completed, got {agent_run.status}")
    if agent_run.finished_at is None:
        raise RuntimeError("the Agent run has no finish timestamp")
    if result.verification is None or not result.verification.sufficient:
        raise RuntimeError("the evidence check did not report a complete evidence base")
    if result.verification.has_prior_baseline is not True:
        raise RuntimeError("the prior report baseline was never queried")
    if not result.verification.content_repo_ids:
        raise RuntimeError("no cached repository content was read")

    indexes = [step.step_index for step in steps]
    if indexes != list(range(1, len(steps) + 1)):
        raise RuntimeError(f"step indexes are not contiguous: {indexes}")
    step_types = {step.step_type for step in steps}
    if step_types != {"plan", "tool", "observe", "verify", "write"}:
        raise RuntimeError(f"unexpected step types: {sorted(step_types)}")
    if any(not step.summary for step in steps):
        raise RuntimeError("a step was recorded without a summary")

    if not calls:
        raise RuntimeError("no tool calls were recorded")
    if any(call.agent_run_id != result.agent_run_id for call in calls):
        raise RuntimeError("a tool call was recorded against a different Agent run")
    if {call.status for call in calls} != {"success"}:
        raise RuntimeError(f"unexpected tool call statuses: {sorted({c.status for c in calls})}")
    call_names = {call.tool_name for call in calls}
    if call_names != {
        "get_repository_profile",
        "query_repo_metrics",
        "search_prior_reports",
        "get_repository_content",
        "save_report_section",
    }:
        raise RuntimeError(f"unexpected tool names: {sorted(call_names)}")

    if len(sections) != 6:
        raise RuntimeError(f"expected six draft sections, got {len(sections)}")
    if {section.version for section in sections} != {1}:
        raise RuntimeError("a first draft section did not start at version 1")
    if any(not section.citations_json for section in sections):
        raise RuntimeError("a draft section was saved without citations")
    summary_section = next(section for section in sections if section.section_key == "summary")
    if "stars" not in summary_section.content_markdown:
        raise RuntimeError("the summary section does not carry the metric numbers")

    snapshots_after = _count(session, RepoSnapshot)
    contents_after = _count(session, RepositoryContent)
    repositories_after = _count(session, Repository)
    if (snapshots_before, contents_before, repositories_before) != (
        snapshots_after,
        contents_after,
        repositories_after,
    ):
        raise RuntimeError("an Agent read tool wrote domain rows")

    no_candidate = run_research_agent(
        session,
        topic_id=empty_topic_id,
        settings=settings,
        now=SMOKE_NOW,
    )
    if no_candidate.status != "needs_review" or no_candidate.error_code != AGENT_NO_CANDIDATES:
        raise RuntimeError(
            f"empty topic did not degrade cleanly: {no_candidate.status}/{no_candidate.error_code}"
        )
    if no_candidate.section_count != 0:
        raise RuntimeError("an empty topic still produced draft sections")

    unavailable = run_research_agent(
        session,
        topic_id=topic_id,
        settings=settings,
        client=UnavailableClient(),
        now=SMOKE_NOW,
    )
    if unavailable.status != "failed" or unavailable.error_code != AGENT_MODEL_UNAVAILABLE:
        raise RuntimeError(
            f"model failure did not fail cleanly: {unavailable.status}/{unavailable.error_code}"
        )

    return {
        "status": result.status,
        "error_code": result.error_code,
        "pipeline_status": pipeline_run.status,
        "agent_run_rows": _count(session, AgentRun),
        "step_count": len(steps),
        "step_types": sorted(step_types),
        "step_indexes_contiguous": indexes == list(range(1, len(steps) + 1)),
        "tool_call_rows": len(calls),
        "tool_call_names": sorted(call_names),
        "tool_call_run_linked": all(call.agent_run_id == result.agent_run_id for call in calls),
        "tool_call_statuses": sorted({call.status for call in calls}),
        "draft_rows": _count(session, ReportDraft),
        "section_rows": len(sections),
        "section_keys": sorted(section.section_key for section in sections),
        "section_versions": sorted({section.version for section in sections}),
        "verification_sufficient": result.verification.sufficient,
        "verification_prior_baseline": result.verification.has_prior_baseline,
        "verification_content_repos": len(result.verification.content_repo_ids),
        "loop_count": result.loop_count,
        "model_call_count": result.model_call_count,
        "candidate_count": len(result.candidate_repo_ids),
        "second_run_status": second.status,
        "snapshot_rows_before": snapshots_before,
        "snapshot_rows_after": snapshots_after,
        "content_rows_before": contents_before,
        "content_rows_after": contents_after,
        "repository_rows_before": repositories_before,
        "repository_rows_after": repositories_after,
        "no_candidate_status": no_candidate.status,
        "no_candidate_error": no_candidate.error_code,
        "no_candidate_sections": no_candidate.section_count,
        "model_failure_status": unavailable.status,
        "model_failure_error": unavailable.error_code,
    }


def main() -> None:
    """Run the T8 smoke checks on real MySQL and print the assertion payload."""
    args = parse_args()
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    if args.cleanup_only:
        with session_factory() as session:
            cleanup_verification_records(session)
        engine.dispose()
        print(
            json.dumps(
                {"cleanup_only": True, "verifier": VERIFIER_USERNAME},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return

    payload: dict[str, Any] = {}
    try:
        with session_factory() as session:
            cleanup_verification_records(session)
            ids = seed_topics(session)
            payload = run_checks(session, ids)
    finally:
        with session_factory() as session:
            cleanup_verification_records(session)
        engine.dispose()

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
