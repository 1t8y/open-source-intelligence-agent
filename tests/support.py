"""整个测试套件共享的种子数据辅助与配置构造器。"""

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
    """为内存版 Agent 用例构造已校验的配置，且不读取 `.env`。

    ``_env_file=None`` 是承重的，不是洁癖。``Settings`` 会从工作目录
    读 ``.env``，而开发者本机的 ``.env`` 指向真实供应商
    （``AGENT_PROVIDER=openai_compatible``），少了这个参数，
    套件就会构造出真实的 HTTP 客户端、消耗真实额度，
    并对着一个测试从未打算使用的供应商做断言。
    需要指定供应商的调用方显式传入即可，它依然优先于默认值。
    """
    values: dict[str, Any] = {
        "database_url": "mysql+pymysql://app:secret@127.0.0.1:3306/open_source_intelligence",
        "redis_url": "redis://127.0.0.1:6379/0",
        "secret_key": TEST_SECRET_KEY,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def build_session_factory() -> sessionmaker[Session]:
    """构造一个共享的内存数据库工厂。"""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Workspace:
    """构造一致的授权关系与数据图的种子数据辅助。"""

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
        """创建一个归共享测试用户所有的主题。"""
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
        """创建一个数据源。"""
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
        """创建一个带最新计数器的仓库。"""
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
        """把仓库挂到主题下，作为一个带排名的候选。"""
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
        """创建一条每日快照。"""
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
        """为仓库缓存一份已清洗的 README 内容。"""
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
        """创建配置、流水线运行与 Agent 运行这条链路。"""
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

    def add_agent_step(
        self,
        agent_run_id: int,
        *,
        step_type: str = "tool",
        status: str = "ok",
        summary: str = "executed 1 tool call(s)",
        step_index: int = 1,
    ) -> AgentStep:
        """给一次运行追加一步，供需要读取轨迹的用例使用。"""
        step = AgentStep(
            agent_run_id=agent_run_id,
            step_index=step_index,
            step_type=step_type,
            status=status,
            summary=summary,
            latency_ms=1,
        )
        self.session.add(step)
        self.session.flush()
        return step

    def add_tool_call(
        self,
        agent_run_id: int,
        agent_step_id: int | None,
        *,
        tool_name: str = "get_repository_profile",
        status: str = "success",
        arguments: dict[str, object] | None = None,
        result_summary: str = "1 row",
    ) -> ToolCall:
        """追加一次工具调用，可选择挂到发起它的那一步上。"""
        call = ToolCall(
            agent_run_id=agent_run_id,
            agent_step_id=agent_step_id,
            tool_name=tool_name,
            arguments_json=arguments or {"repo_id": 1},
            result_summary=result_summary,
            status=status,
            latency_ms=1,
        )
        self.session.add(call)
        self.session.flush()
        return call

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
        """创建一份报告，并可附带引用行。"""
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
        """为给定的运行与主题构造一份工具上下文。"""
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
        """创建一个在白名单内的投递目标。

        两个 `allow_*` 开关默认取 `False`，与列默认值保持一致：
        新加进来的投递目标在有人刻意放宽之前都是安全的。
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
