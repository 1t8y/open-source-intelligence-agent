"""播种「AI 最新技术」热门工具追踪主题，用于演示 hot_tool 报告类型。

与 `seed_demo.py` 互补：那份脚本播的是评估数据集绑定的趋势主题
（`sweep-llm-agent-topic`，report_type=trend），本脚本播的是新报告的
演示形态 —— 一个产出「热门工具追踪」报告的主题，关键词覆盖 skill /
plugin / agent，候选仓库的 README 缓存刻意写成「核心功能」导向，好让
研究 Agent 按 hot_tool 的五节结构逐仓库提炼核心功能。

幂等：账号、数据源、主题、仓库、README 全部先查后建，可以安全重跑。

在 api 容器内运行（与 `seed_demo.py` 相同）：

    docker compose --env-file .env --env-file .env.db -f infra/docker-compose.yml \
      exec -T api python /app/scripts/seed_hot_tool_topic.py
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    RepositoryContent,
    RepoSnapshot,
    Topic,
    TopicRepository,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.security.bootstrap import bootstrap_admin
from open_source_intelligence.security.service import get_user_by_username

#: 演示主题名：AI 最新技术（skill / plugin / agent）。
TOPIC_NAME = "ai-latest-tech-topic"

#: 报告类型：热门工具追踪。
REPORT_TYPE = "hot_tool"


class RepositorySpec(TypedDict):
    """一个热门 AI 工具仓库的种子形态。

    `readme` 是研究 Agent 读取的缓存正文，刻意写成「核心功能」导向：
    用清晰的「Core capabilities」小节点名该工具解决什么问题、怎么用，
    这正是 hot_tool 报告「tools / capabilities」两节要提炼的证据。

    `stars`/`forks` 是演示用占位计数（不是编造的采集历史，见 `_ensure_snapshots`
    的说明）；`daily_delta` 是快照序列的每日 stars 增量。
    """

    full_name: str
    html_url: str
    description: str
    language: str
    stars: int
    forks: int
    daily_delta: int
    readme: str


#: 五个热门 AI 工具仓库。stars/forks 是演示占位值（不是真实采集计数）；README
#: 正文承载「核心功能」。与 `seed_demo.py` 的评估绑定主题不同，本主题刻意写一份
#: 7 天的快照序列：hot_tool 报告要展示热度数字，而报告验证器要求含数字的章节必须
#: 带 metric 引用（`NUMBER_WITHOUT_METRIC`），空快照会让演示报告停在「验证失败」。
REPOSITORIES: tuple[RepositorySpec, ...] = (
    {
        "full_name": "cline/cline",
        "html_url": "https://github.com/cline/cline",
        "description": "Autonomous coding agent that runs in your IDE.",
        "language": "TypeScript",
        "stars": 30000,
        "forks": 3200,
        "daily_delta": 400,
        "readme": (
            "# Cline\n\nAn autonomous coding agent that lives in your editor.\n\n"
            "## Core capabilities\n"
            "- Plan and execute multi-file edits from a single prompt\n"
            "- Read project files, run commands, and inspect diffs in a bounded loop\n"
            "- Pluggable model backends (OpenAI, Anthropic, local models)\n"
        ),
    },
    {
        "full_name": "anthropics/claude-code",
        "html_url": "https://github.com/anthropics/claude-code",
        "description": "Terminal-based agentic coding tool with skills.",
        "language": "TypeScript",
        "stars": 50000,
        "forks": 4100,
        "daily_delta": 500,
        "readme": (
            "# Claude Code\n\nA terminal-based agentic coding tool.\n\n"
            "## Core capabilities\n"
            "- Interactive coding sessions with file editing and command execution\n"
            "- Reusable skill definitions that package domain knowledge\n"
            "- Plugin hooks for custom workflows and model routing\n"
        ),
    },
    {
        "full_name": "openai/openai-agents-python",
        "html_url": "https://github.com/openai/openai-agents-python",
        "description": "Lightweight framework for multi-agent workflows.",
        "language": "Python",
        "stars": 25000,
        "forks": 2100,
        "daily_delta": 300,
        "readme": (
            "# OpenAI Agents SDK\n\nA lightweight framework for building multi-agent workflows.\n\n"
            "## Core capabilities\n"
            "- Define agents as small, reusable units with handoffs\n"
            "- Guardrails that validate inputs and outputs before they propagate\n"
            "- Built-in tracing and session management\n"
        ),
    },
    {
        "full_name": "browser-use/browser-use",
        "html_url": "https://github.com/browser-use/browser-use",
        "description": "Make websites accessible for AI agents.",
        "language": "Python",
        "stars": 35000,
        "forks": 2900,
        "daily_delta": 450,
        "readme": (
            "# Browser Use\n\nMake websites accessible for AI agents.\n\n"
            "## Core capabilities\n"
            "- Turn web pages into structured, agent-readable elements\n"
            "- Multi-step browser tasks with vision and DOM grounding\n"
            "- Persist sessions and replay agent traces for debugging\n"
        ),
    },
    {
        "full_name": "langchain-ai/langgraph",
        "html_url": "https://github.com/langchain-ai/langgraph",
        "description": "Build stateful, multi-actor agent applications.",
        "language": "Python",
        "stars": 20000,
        "forks": 1800,
        "daily_delta": 250,
        "readme": (
            "# LangGraph\n\nBuild stateful, multi-actor agent applications.\n\n"
            "## Core capabilities\n"
            "- Model agent workflows as explicit graphs with checkpointing\n"
            "- Human-in-the-loop interrupts and time-travel debugging\n"
            "- Durable memory across long-running, multi-turn sessions\n"
        ),
    },
)

_DEV_ADMIN_PASSWORD = "dev-admin-password-change-me"


def _now_utc() -> datetime:
    """返回当前 UTC 时间；每次运行只钉一次。"""
    return datetime.now(UTC)


def _content_hash(content: str) -> str:
    """返回 `repository_contents` 所存的稳定正文摘要。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _ensure_admin(session: Session, admin_password: str) -> User:
    """返回管理员账号；不存在时用 bootstrap 路径创建。"""
    admin = get_user_by_username(session, "admin")
    if admin is None:
        bootstrap_admin(
            session,
            username="admin",
            password=admin_password,
            display_name="运维管理员",
        )
        session.commit()
        admin = get_user_by_username(session, "admin")
    assert admin is not None
    return admin


def _ensure_source(session: Session) -> DataSource:
    """返回那个唯一的 GitHub 数据源；首次使用时创建它。"""
    existing = session.scalar(select(DataSource).where(DataSource.source_type == "github"))
    if existing is not None:
        return existing
    source = DataSource(
        name="github",
        source_type="github",
        base_url="https://github.com",
        enabled=True,
        rate_limit_per_minute=30,
    )
    session.add(source)
    session.flush()
    return source


def _ensure_topic(session: Session, admin: User) -> Topic:
    """返回热门工具追踪演示主题；首次使用时创建它。"""
    existing = session.scalar(select(Topic).where(Topic.name == TOPIC_NAME))
    if existing is not None:
        # 重跑时把报告类型纠正回来，避免旧主题停留在默认 trend。
        existing.report_type = REPORT_TYPE
        session.commit()
        return existing
    topic = Topic(
        name=TOPIC_NAME,
        description="追踪最新的 AI 工具（skill / 插件 / agent），盘点热门仓库的核心功能。",
        keywords_json=["skill", "plugin", "agent"],
        min_stars=0,
        window_days=7,
        max_candidates=10,
        report_type=REPORT_TYPE,
        publish_mode="AUTO_WITH_DELAY",
        enabled=True,
        created_by=admin.id,
    )
    session.add(topic)
    session.flush()
    return topic


def _bind_candidate(
    session: Session,
    *,
    topic: Topic,
    repo: Repository,
    rank: int,
) -> None:
    """把仓库排进主题，并把这条关联钉住（防每日排名清扫替换夹具）。"""
    link = session.get(TopicRepository, (topic.id, repo.id))
    if link is None:
        session.add(
            TopicRepository(
                topic_id=topic.id,
                repository_id=repo.id,
                score=Decimal("100.0") - Decimal(rank),
                rank=rank,
                selected_for_agent=True,
                manually_pinned=True,
            )
        )
        return
    link.rank = rank
    link.selected_for_agent = True
    link.manually_pinned = True


def _ensure_repositories(session: Session, source: DataSource, topic: Topic) -> list[Repository]:
    """建出候选仓库与 README 缓存，并排进主题。"""
    moment = _now_utc()
    created: list[Repository] = []
    seeded_ids: set[int] = set()
    for index, spec in enumerate(REPOSITORIES, start=1):
        repo = session.scalar(
            select(Repository).where(
                Repository.source_id == source.id,
                Repository.full_name == spec["full_name"],
            )
        )
        if repo is None:
            repo = Repository(
                source_id=source.id,
                provider_repo_id=spec["full_name"],
                full_name=spec["full_name"],
                html_url=spec["html_url"],
                description=spec["description"],
                language=spec["language"],
                stars=spec["stars"],
                forks=spec["forks"],
                first_seen_at=moment,
                last_seen_at=moment,
            )
            session.add(repo)
            session.flush()

        # README 缓存；先查后建。
        exists = session.scalar(
            select(RepositoryContent.id).where(
                RepositoryContent.repository_id == repo.id,
                RepositoryContent.content_type == "readme",
            )
        )
        if exists is None:
            session.add(
                RepositoryContent(
                    repository_id=repo.id,
                    content_type="readme",
                    source_url=f"{spec['html_url']}/blob/main/README.md",
                    content=spec["readme"],
                    content_hash=_content_hash(spec["readme"]),
                    fetched_at=moment,
                )
            )

        _bind_candidate(session, topic=topic, repo=repo, rank=index)
        seeded_ids.add(repo.id)
        created.append(repo)

    # 把主题上其余关联的选中标记清掉，研究运行只确定性地读这五个种子候选。
    for stale in session.scalars(
        select(TopicRepository).where(
            TopicRepository.topic_id == topic.id,
            TopicRepository.repository_id.notin_(seeded_ids),
            TopicRepository.selected_for_agent.is_(True),
        )
    ).all():
        stale.selected_for_agent = False

    session.commit()
    return created


#: 快照序列覆盖的天数（含今天）。研究窗口默认 7 天，写 7 个点正好填满。
SNAPSHOT_DAYS = 7


def _ensure_snapshots(session: Session, repositories: list[Repository]) -> None:
    """为每个种子仓库写一份 7 天的演示快照序列（幂等）。

    与 `seed_demo.py` 的「种子不写快照」不同，hot_tool 报告的验证器要求含数字的
    章节带 metric 引用（`NUMBER_WITHOUT_METRIC`），空快照会让演示报告停在验证失败。
    这里的序列是演示占位值：stars 从 `stars - daily_delta * (天数-1)` 递增到今天的主表
    计数，每天净增 `daily_delta`，可被 GitHub 之外的任何人一眼认出是演示数据。

    按 `(repository_id, snapshot_date)` 唯一去重，重跑不重复插入。
    """
    today = _now_utc().date()
    specs = {spec["full_name"]: spec for spec in REPOSITORIES}
    for repo in repositories:
        spec = specs.get(repo.full_name)
        if spec is None:
            continue
        delta = spec["daily_delta"]
        fork_delta = max(1, spec["forks"] // 100)
        for offset in range(SNAPSHOT_DAYS - 1, -1, -1):
            day = today - timedelta(days=offset)
            existing = session.scalar(
                select(RepoSnapshot.id).where(
                    RepoSnapshot.repository_id == repo.id,
                    RepoSnapshot.snapshot_date == day,
                )
            )
            if existing is not None:
                continue
            session.add(
                RepoSnapshot(
                    repository_id=repo.id,
                    snapshot_date=day,
                    stars=spec["stars"] - delta * offset,
                    forks=spec["forks"] - fork_delta * offset,
                    stars_delta=delta,
                    forks_delta=fork_delta,
                )
            )
        # 主表计数与最新快照保持一致，避免「主表 star 数」与「快照末点」对不上。
        repo.stars = spec["stars"]
        repo.forks = spec["forks"]
        repo.last_seen_at = _now_utc()
    session.commit()


def main() -> int:
    """播好热门工具追踪主题，并为每个写下的实体打印一行摘要。"""
    admin_password = os.environ.get("SEED_ADMIN_PASSWORD", _DEV_ADMIN_PASSWORD)
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            admin = _ensure_admin(session, admin_password)
            source = _ensure_source(session)
            topic = _ensure_topic(session, admin)
            repositories = _ensure_repositories(session, source, topic)
            _ensure_snapshots(session, repositories)

            print(f"topic: {topic.name} id={topic.id} report_type={topic.report_type}")
            for repo in repositories:
                readme = session.scalar(
                    select(RepositoryContent).where(RepositoryContent.repository_id == repo.id)
                )
                print(
                    f"repository: {repo.full_name} id={repo.id} "
                    f"language={repo.language} readme={'yes' if readme else 'NO'}"
                )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
