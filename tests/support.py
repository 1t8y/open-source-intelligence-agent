"""Shared seeding helpers and settings builders for the test suite."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from open_source_intelligence.agent.tools.context import ToolContext
from open_source_intelligence.core.config import Settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.base import Base
from open_source_intelligence.persistence.models.agent import AgentConfig, AgentRun
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
from open_source_intelligence.persistence.models.notifications import PushTarget
from open_source_intelligence.persistence.models.reporting import Report, ReportSource

ALL_TOOLS = (
    "query_repo_metrics",
    "get_repository_profile",
    "get_repository_content",
    "search_prior_reports",
    "save_report_section",
)

READ_TOOLS = ALL_TOOLS[:4]

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)

TEST_SECRET_KEY = "test-secret-key-with-at-least-32-characters"


def build_settings(**overrides: Any) -> Settings:
    """Build validated settings for in-memory Agent tests without reading `.env`.

    ``_env_file=None`` is load-bearing, not tidiness. ``Settings`` reads ``.env`` from the
    working directory, and a developer's ``.env`` points at a real provider
    (``AGENT_PROVIDER=openai_compatible``), so without this the suite would build real HTTP
    clients, spend real quota, and assert against a provider that tests never meant to use.
    Callers that want a provider pass it explicitly, which still wins over the default.
    """
    values: dict[str, Any] = {
        "database_url": "mysql+pymysql://app:secret@127.0.0.1:3306/open_source_intelligence",
        "redis_url": "redis://127.0.0.1:6379/0",
        "secret_key": TEST_SECRET_KEY,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def build_session_factory() -> sessionmaker[Session]:
    """Build a shared in-memory database factory."""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Workspace:
    """Seed helper that builds a consistent authorization and data graph."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._counter = 0
        self._user: User | None = None

    def _next(self) -> int:
        self._counter += 1
        return self._counter

    def _ensure_user(self) -> User:
        if self._user is None:
            self._user = User(
                id=1,
                username="analyst",
                password_hash="hash",
                display_name="Analyst",
            )
            self.session.add(self._user)
            self.session.flush()
        return self._user

    def add_topic(self, *, name: str = "AI Agent", enabled: bool = True) -> Topic:
        """Create one topic owned by the shared test user."""
        user = self._ensure_user()
        topic = Topic(
            name=name,
            min_stars=0,
            window_days=7,
            max_candidates=10,
            publish_mode="AUTO_WITH_DELAY",
            enabled=enabled,
            created_by=user.id,
        )
        self.session.add(topic)
        self.session.flush()
        return topic

    def add_source(self, *, source_type: str = "github") -> DataSource:
        """Create one data source."""
        source = DataSource(
            name=source_type,
            source_type=source_type,
            base_url=f"https://{source_type}.example.com",
            enabled=True,
            rate_limit_per_minute=30,
        )
        self.session.add(source)
        self.session.flush()
        return source

    def add_repository(
        self,
        *,
        source_id: int,
        provider_repo_id: str = "1",
        full_name: str = "acme/rocket",
        stars: int = 100,
        forks: int = 10,
        language: str | None = "Python",
        topics: Sequence[str] | None = None,
        description: str | None = "A test repository",
    ) -> Repository:
        """Create one repository with latest counters."""
        repository = Repository(
            source_id=source_id,
            provider_repo_id=provider_repo_id,
            full_name=full_name,
            html_url=f"https://example.com/{full_name}",
            description=description,
            language=language,
            license="MIT",
            topics_json=list(topics) if topics is not None else ["ai", "llm"],
            stars=stars,
            forks=forks,
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
        self.session.add(repository)
        self.session.flush()
        return repository

    def link(
        self,
        topic_id: int,
        repository_id: int,
        *,
        rank: int = 1,
        score: Decimal = Decimal("1"),
        selected: bool = True,
    ) -> TopicRepository:
        """Link a repository to a topic as a ranked candidate."""
        link = TopicRepository(
            topic_id=topic_id,
            repository_id=repository_id,
            score=score,
            rank=rank,
            selected_for_agent=selected,
        )
        self.session.add(link)
        self.session.flush()
        return link

    def add_snapshot(
        self,
        repository_id: int,
        snapshot_date: date,
        *,
        stars: int,
        forks: int,
        open_issues: int | None = None,
        stars_delta: int | None = None,
        forks_delta: int | None = None,
    ) -> RepoSnapshot:
        """Create one daily snapshot."""
        snapshot = RepoSnapshot(
            repository_id=repository_id,
            snapshot_date=snapshot_date,
            stars=stars,
            forks=forks,
            open_issues=open_issues,
            stars_delta=stars_delta,
            forks_delta=forks_delta,
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot

    def add_content(
        self,
        repository_id: int,
        *,
        content: str,
        source_url: str = "https://example.com/readme",
    ) -> RepositoryContent:
        """Cache cleaned README content for a repository."""
        record = RepositoryContent(
            repository_id=repository_id,
            content_type="readme",
            source_url=source_url,
            content=content,
            content_hash=f"hash-{self._next()}",
            fetched_at=NOW,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def add_agent_run(self, topic_id: int) -> AgentRun:
        """Create the config, pipeline run, and agent run chain."""
        index = self._next()
        config = AgentConfig(
            name=f"default-{index}",
            version="v1",
            provider="openai",
            model="gpt-4o-mini",
            prompt_version="p1",
            tool_policy_json={"tools": list(ALL_TOOLS)},
            max_loops=4,
            max_model_calls=6,
            max_tool_calls=10,
            max_cost=Decimal("1"),
            enabled=True,
        )
        self.session.add(config)
        self.session.flush()
        pipeline_run = PipelineRun(
            topic_id=topic_id,
            run_type="agent",
            status="succeeded",
            attempt=1,
            started_at=NOW,
            finished_at=NOW,
        )
        self.session.add(pipeline_run)
        self.session.flush()
        agent_run = AgentRun(
            pipeline_run_id=pipeline_run.id,
            topic_id=topic_id,
            agent_config_id=config.id,
            status="running",
            window_start=NOW,
            window_end=NOW,
            started_at=NOW,
        )
        self.session.add(agent_run)
        self.session.flush()
        return agent_run

    def add_report(
        self,
        *,
        topic_id: int,
        agent_run_id: int,
        title: str = "Weekly report",
        content: str = "Summary of the week",
        generated_at: datetime = NOW,
        status: str = "published",
        repository_id: int | None = None,
        source_refs: Sequence[str] = (),
        section_key: str = "summary",
    ) -> Report:
        """Create one report with optional citation rows."""
        report = Report(
            agent_run_id=agent_run_id,
            topic_id=topic_id,
            title=title,
            current_version=1,
            content_markdown=content,
            status=status,
            visibility="TEAM",
            sensitivity="INTERNAL",
            publish_mode="AUTO_WITH_DELAY",
            generated_at=generated_at,
        )
        self.session.add(report)
        self.session.flush()
        for ref in source_refs:
            self.session.add(
                ReportSource(
                    report_id=report.id,
                    repository_id=repository_id,
                    section_key=section_key,
                    source_type="metric",
                    source_ref=ref,
                )
            )
        self.session.flush()
        return report

    def context(
        self,
        topic_id: int,
        *,
        agent_run_id: int,
        authorized: Sequence[int] | None = None,
        allowed: Sequence[str] = ALL_TOOLS,
        max_tool_calls: int = 10,
        tool_call_count: int = 0,
    ) -> ToolContext:
        """Build a tool context for the given run and topic."""
        authorized_ids = {topic_id} if authorized is None else set(authorized)
        return ToolContext(
            agent_run_id=agent_run_id,
            topic_id=topic_id,
            authorized_topic_ids=frozenset(authorized_ids),
            allowed_tools=tuple(allowed),
            max_tool_calls=max_tool_calls,
            tool_call_count=tool_call_count,
        )

    def add_push_target(
        self,
        *,
        name: str | None = None,
        channel: str = "email",
        destination: str = "ops@example.com",
        enabled: bool = True,
        allow_private: bool = False,
        allow_confidential: bool = False,
    ) -> PushTarget:
        """Create one allowlisted delivery destination.

        The two `allow_*` switches default to `False`, mirroring the column defaults: a
        freshly added destination is safe until someone deliberately widens it.
        """
        target = PushTarget(
            name=name or f"target-{self._next()}",
            channel=channel,
            destination=destination,
            enabled=enabled,
            allow_private=allow_private,
            allow_confidential=allow_confidential,
        )
        self.session.add(target)
        self.session.flush()
        return target
