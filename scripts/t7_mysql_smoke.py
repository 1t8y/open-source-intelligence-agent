"""Run T7 Agent tool layer smoke checks against MySQL.

The script seeds a temporary user, topics, repositories, content, an Agent run
chain, and a prior report, then invokes all five whitelisted tools through the
real registry. It verifies schema handling, ACL denial, truncation, the
untrusted marker, draft idempotency, read-only behaviour, and tool call
recording on a real database rather than in-memory SQLite.
"""

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.tools.context import ToolContext
from open_source_intelligence.agent.tools.registry import (
    WHITELIST,
    build_default_registry,
)
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.models.agent import (
    AgentConfig,
    AgentRun,
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

VERIFIER_USERNAME = "__t7_verifier__"
AUTHORIZED_TOPIC = "__T7 Tool Topic__"
OUT_OF_SCOPE_TOPIC = "__T7 Out Of Scope Topic__"
PROVIDER_PREFIX = "__t7_smoke_"
CONFIG_PREFIX = "__t7_smoke_"
CONTENT_SOURCE_URL = "https://example.com/__t7_smoke__/readme"
SCRIPT_MARKER = "evil_payload()"
NOW = datetime.now(UTC)


def cleanup_verification_records(session: Session) -> None:
    """Remove every record created by a previous T7 smoke run."""
    topic_ids = list(
        session.scalars(
            select(Topic.id).where(Topic.name.in_((AUTHORIZED_TOPIC, OUT_OF_SCOPE_TOPIC)))
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
                session.execute(delete(AgentRun).where(AgentRun.id.in_(agent_run_ids)))
            session.execute(delete(PipelineRun).where(PipelineRun.id.in_(run_ids)))
        session.execute(delete(TopicRepository).where(TopicRepository.topic_id.in_(topic_ids)))
        session.execute(delete(Topic).where(Topic.id.in_(topic_ids)))
    session.execute(
        delete(Repository).where(
            Repository.provider_repo_id.startswith(PROVIDER_PREFIX, autoescape=True)
        )
    )
    session.execute(
        delete(AgentConfig).where(AgentConfig.name.startswith(CONFIG_PREFIX, autoescape=True))
    )
    session.execute(delete(User).where(User.username == VERIFIER_USERNAME))
    session.commit()


def parse_args() -> argparse.Namespace:
    """Parse the optional cleanup mode used by the verification script."""
    parser = argparse.ArgumentParser(description="Run T7 Agent tool smoke checks")
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
            name="__t7_smoke_github__",
            source_type="github",
            base_url="https://api.github.com",
            enabled=True,
            rate_limit_per_minute=30,
        )
        session.add(source)
        session.flush()
    return source


def main() -> None:
    """Invoke every whitelisted tool on real MySQL and assert the contract."""
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

    result: dict[str, Any] = {}
    try:
        with session_factory() as session:
            cleanup_verification_records(session)
            user = User(
                username=VERIFIER_USERNAME,
                password_hash="t7-verification-only",
                display_name="T7 Verifier",
                status="active",
                data_scope="team",
            )
            session.add(user)
            session.flush()
            authorized_topic = Topic(
                name=AUTHORIZED_TOPIC,
                min_stars=0,
                window_days=7,
                max_candidates=10,
                publish_mode="AUTO_WITH_DELAY",
                enabled=True,
                created_by=user.id,
            )
            out_of_scope_topic = Topic(
                name=OUT_OF_SCOPE_TOPIC,
                min_stars=0,
                window_days=7,
                max_candidates=10,
                publish_mode="AUTO_WITH_DELAY",
                enabled=True,
                created_by=user.id,
            )
            session.add_all([authorized_topic, out_of_scope_topic])
            session.flush()

            source = _seed_source(session)
            in_scope_repo = Repository(
                source_id=source.id,
                provider_repo_id=f"{PROVIDER_PREFIX}in_scope",
                full_name="__t7_smoke__/in-scope",
                html_url="https://example.com/__t7_smoke__/in-scope",
                description="In-scope repository",
                language="Python",
                license="MIT",
                topics_json=["ai"],
                stars=1500,
                forks=120,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
            out_of_scope_repo = Repository(
                source_id=source.id,
                provider_repo_id=f"{PROVIDER_PREFIX}out_of_scope",
                full_name="__t7_smoke__/out-of-scope",
                html_url="https://example.com/__t7_smoke__/out-of-scope",
                description="Out-of-scope repository",
                language="Python",
                license="MIT",
                topics_json=["ai"],
                stars=10,
                forks=1,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
            session.add_all([in_scope_repo, out_of_scope_repo])
            session.flush()
            session.add_all(
                [
                    TopicRepository(
                        topic_id=authorized_topic.id,
                        repository_id=in_scope_repo.id,
                        score=1,
                        rank=1,
                        selected_for_agent=True,
                    ),
                    TopicRepository(
                        topic_id=out_of_scope_topic.id,
                        repository_id=out_of_scope_repo.id,
                        score=1,
                        rank=1,
                        selected_for_agent=True,
                    ),
                ]
            )
            session.add_all(
                [
                    RepoSnapshot(
                        repository_id=in_scope_repo.id,
                        snapshot_date=date(2099, 3, 1),
                        stars=1400,
                        forks=110,
                        stars_delta=10,
                        forks_delta=1,
                    ),
                    RepoSnapshot(
                        repository_id=in_scope_repo.id,
                        snapshot_date=date(2099, 3, 2),
                        stars=1500,
                        forks=120,
                        stars_delta=100,
                        forks_delta=10,
                    ),
                ]
            )
            session.add(
                RepositoryContent(
                    repository_id=in_scope_repo.id,
                    content_type="readme",
                    source_url=CONTENT_SOURCE_URL,
                    content=f"# In scope\n<script>{SCRIPT_MARKER}</script>\n" + ("detail " * 400),
                    content_hash="__t7_smoke__",
                    fetched_at=NOW,
                )
            )
            config = AgentConfig(
                name=f"{CONFIG_PREFIX}config",
                version="v1",
                provider="openai",
                model="gpt-4o-mini",
                prompt_version="p1",
                tool_policy_json={"tools": ["query_repo_metrics"]},
                max_loops=4,
                max_model_calls=6,
                max_tool_calls=10,
                max_cost=1,
                enabled=True,
            )
            session.add(config)
            session.flush()
            pipeline_run = PipelineRun(
                topic_id=authorized_topic.id,
                run_type="agent",
                status="running",
                attempt=1,
                started_at=NOW,
            )
            session.add(pipeline_run)
            session.flush()
            agent_run = AgentRun(
                pipeline_run_id=pipeline_run.id,
                topic_id=authorized_topic.id,
                agent_config_id=config.id,
                status="running",
                window_start=NOW - timedelta(days=7),
                window_end=NOW,
                started_at=NOW,
            )
            session.add(agent_run)
            session.flush()
            prior_report = Report(
                agent_run_id=agent_run.id,
                topic_id=authorized_topic.id,
                title="T7 prior report",
                current_version=1,
                content_markdown="Previous week stars grew steadily.",
                status="published",
                visibility="TEAM",
                sensitivity="INTERNAL",
                publish_mode="AUTO_WITH_DELAY",
                generated_at=NOW - timedelta(days=7),
            )
            session.add(prior_report)
            session.flush()
            session.add(
                ReportSource(
                    report_id=prior_report.id,
                    repository_id=in_scope_repo.id,
                    section_key="summary",
                    source_type="metric",
                    source_ref="__t7_smoke__#stars",
                )
            )
            session.commit()

            topic_id = authorized_topic.id
            other_topic_id = out_of_scope_topic.id
            repo_id = in_scope_repo.id
            run_id = agent_run.id

        registry = build_default_registry()
        with session_factory() as session:
            ctx = ToolContext(
                agent_run_id=run_id,
                topic_id=topic_id,
                authorized_topic_ids=frozenset({topic_id}),
                allowed_tools=WHITELIST,
                max_tool_calls=10,
            )
            snapshots_before = session.scalar(select(func.count(RepoSnapshot.id))) or 0

            profile = registry.invoke("get_repository_profile", ctx, session, {"repo_id": repo_id})
            metrics = registry.invoke(
                "query_repo_metrics",
                ctx,
                session,
                {
                    "repo_id": repo_id,
                    "start": datetime(2099, 3, 1, tzinfo=UTC),
                    "end": datetime(2099, 3, 5, tzinfo=UTC),
                },
            )
            content = registry.invoke(
                "get_repository_content",
                ctx,
                session,
                {"repo_id": repo_id, "max_chars": 500},
            )
            prior = registry.invoke(
                "search_prior_reports",
                ctx,
                session,
                {"topic_id": topic_id, "before": NOW + timedelta(hours=1)},
            )
            denied = registry.invoke(
                "search_prior_reports",
                ctx,
                session,
                {"topic_id": other_topic_id, "before": NOW + timedelta(hours=1)},
            )
            first_section = registry.invoke(
                "save_report_section",
                ctx,
                session,
                {
                    "section_key": "summary",
                    "content_markdown": "## Summary\nIn-scope project is trending.",
                    "content_digest": "smoke-digest-0001",
                    "citations": [{"source_type": "metric", "source_ref": "repo#stars"}],
                },
            )
            repeat_section = registry.invoke(
                "save_report_section",
                ctx,
                session,
                {
                    "section_key": "summary",
                    "content_markdown": "## Summary\nIn-scope project is trending.",
                    "content_digest": "smoke-digest-0001",
                },
            )
            bumped_section = registry.invoke(
                "save_report_section",
                ctx,
                session,
                {
                    "section_key": "summary",
                    "content_markdown": "## Summary\nUpdated wording.",
                    "content_digest": "smoke-digest-0002",
                },
            )
            snapshots_after = session.scalar(select(func.count(RepoSnapshot.id))) or 0
            section_rows = session.scalar(select(func.count(ReportSection.id))) or 0
            draft_rows = session.scalar(select(func.count(ReportDraft.id))) or 0
            calls = list(session.scalars(select(ToolCall).order_by(ToolCall.id)).all())

            if profile.ok is not True or metrics.ok is not True or content.ok is not True:
                raise RuntimeError("a read-only tool failed on real MySQL")
            if profile.data is None or profile.data.provider == "":
                raise RuntimeError("profile payload is incomplete")
            if metrics.meta.result_count != 2:
                raise RuntimeError(f"expected two metric points, got {metrics.meta.result_count}")
            if content.data is None or content.data.truncated is not True:
                raise RuntimeError("content was not truncated")
            if SCRIPT_MARKER in content.data.content or "<script>" in content.data.content:
                raise RuntimeError("content cleaning did not strip the script tag")
            if content.data.untrusted is not True:
                raise RuntimeError("content was not marked untrusted")
            if prior.meta.result_count != 1:
                raise RuntimeError(f"expected one prior report, got {prior.meta.result_count}")
            if denied.ok is not False or denied.error is None:
                raise RuntimeError("out-of-scope topic was not denied")
            if denied.error.code != "TOOL_ACCESS_DENIED":
                raise RuntimeError(f"unexpected denial code: {denied.error.code}")
            if denied.meta.result_count != 0:
                raise RuntimeError("a denied report search leaked a result count")
            if first_section.data is None or first_section.data.version != 1:
                raise RuntimeError("first section save did not create version 1")
            if repeat_section.data is None or repeat_section.data.version != 1:
                raise RuntimeError("repeating a section save created a new version")
            if repeat_section.data.report_draft_id != first_section.data.report_draft_id:
                raise RuntimeError("repeated section save used a different draft")
            if bumped_section.data is None or bumped_section.data.version != 2:
                raise RuntimeError("changed section content did not bump the version")
            if section_rows != 2 or draft_rows != 1:
                raise RuntimeError(
                    f"unexpected section rows: sections={section_rows} drafts={draft_rows}"
                )
            if snapshots_after != snapshots_before:
                raise RuntimeError("a read-only tool changed snapshot rows")
            if len(calls) != 8:
                raise RuntimeError(f"expected eight tool call records, got {len(calls)}")

            result = {
                "profile_ok": profile.ok,
                "profile_provider": profile.data.provider,
                "metric_points": metrics.meta.result_count,
                "content_truncated": content.data.truncated,
                "content_untrusted": content.data.untrusted,
                "content_cleaned": SCRIPT_MARKER not in content.data.content,
                "prior_reports_count": prior.meta.result_count,
                "unauthorized_code": denied.error.code,
                "unauthorized_leaked": denied.meta.result_count != 0,
                "section_first_version": first_section.data.version,
                "section_repeat_version": repeat_section.data.version,
                "section_bump_version": bumped_section.data.version,
                "section_rows": section_rows,
                "draft_rows": draft_rows,
                "snapshot_rows_before": snapshots_before,
                "snapshot_rows_after": snapshots_after,
                "tool_call_rows": len(calls),
                "tool_call_statuses": [call.status for call in calls],
                "tool_call_names": [call.tool_name for call in calls],
                "denied_calls": [call.tool_name for call in calls if call.status == "denied"],
            }
    finally:
        with session_factory() as session:
            cleanup_verification_records(session)
        engine.dispose()

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
