"""报告、API 与 Web 测试共用的种子数据辅助。

它们和 `support.py` 一起放在 `tests/` 顶层，因为 pytest 会把 `tests` 加进
`sys.path`（见 `pyproject.toml` 的 `pythonpath`），每个测试目录才能把它们
当作普通顶层模块导入。如果放进 `tests/reports/` 里做 `conftest.py` 的邻居，
那就只有那一个目录能导入它们了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from itertools import count

from sqlalchemy import select
from sqlalchemy.orm import Session
from support import NOW, Workspace, build_settings

from open_source_intelligence.agent.runtime import run_research_agent
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    Topic,
)
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.reports.composition import generate_report

FIRST_SNAPSHOT_DATE = date(2026, 10, 5)
LAST_SNAPSHOT_DATE = date(2026, 10, 11)

AUTO = "AUTO"
AUTO_WITH_DELAY = "AUTO_WITH_DELAY"
MANUAL = "MANUAL"

#: `topics.name` 有唯一约束，所以每个种子主题都要有自己的名字。
_TOPIC_SEQUENCE = count(1)
#: `repositories(source_id, provider_repo_id)` 有唯一约束，所以 id 不能重复。
_REPO_SEQUENCE = count(1)


@dataclass(slots=True)
class SeededReport:
    """一份已生成的报告，以及它背后的运行、主题与仓库。"""

    report_id: int
    agent_run_id: int
    topic: Topic
    repositories: list[Repository] = field(default_factory=list)

    def reload(self, session: Session) -> Report:
        """返回报告当前的行。"""
        report = session.get(Report, self.report_id)
        assert report is not None
        return report


def _shared_source(workspace: Workspace) -> DataSource:
    """返回唯一的 GitHub 数据源，首次调用时创建。

    `data_sources.source_type` 有唯一约束 —— 每个供应商只留一行 ——
    所以每个种子主题的仓库都挂到同一个数据源上。
    """
    existing = workspace.session.scalar(select(DataSource).order_by(DataSource.id).limit(1))
    if existing is not None:
        return existing
    source = DataSource(
        name="github",
        source_type="github",
        base_url="https://github.example.com",
        enabled=True,
        rate_limit_per_minute=30,
    )
    workspace.session.add(source)
    workspace.session.flush()
    return source


def seed_topic(
    workspace: Workspace,
    *,
    publish_mode: str,
    repo_count: int = 2,
    name: str | None = None,
) -> tuple[Topic, list[Repository]]:
    """创建一个主题，带上排名候选、指标序列与缓存的 README。"""
    topic = workspace.add_topic(name=name or f"AI Agent {next(_TOPIC_SEQUENCE)}")
    topic.publish_mode = publish_mode
    source = _shared_source(workspace)
    repositories: list[Repository] = []
    for index in range(repo_count):
        sequence = next(_REPO_SEQUENCE)
        repository = workspace.add_repository(
            source_id=source.id,
            provider_repo_id=str(sequence),
            full_name=f"acme/repo-{sequence}",
            stars=100 + index,
        )
        workspace.link(topic.id, repository.id, rank=index + 1)
        workspace.add_snapshot(repository.id, FIRST_SNAPSHOT_DATE, stars=100 + index, forks=10)
        workspace.add_snapshot(repository.id, LAST_SNAPSHOT_DATE, stars=140 + index, forks=12)
        workspace.add_content(
            repository.id,
            content="# README\nA cached repository readme used by the publishing tests.",
        )
        repositories.append(repository)
    workspace.session.flush()
    return topic, repositories


def run_agent_once(session: Session, topic_id: int) -> int:
    """完整跑一次确定性的 Agent，并返回 Agent 运行 id。"""
    result = run_research_agent(
        session,
        topic_id=topic_id,
        settings=build_settings(),
        now=NOW,
    )
    assert result.status == "completed"
    return result.agent_run_id


def build_report(
    session: Session,
    workspace: Workspace,
    *,
    publish_mode: str,
    repo_count: int = 2,
) -> SeededReport:
    """灌一个主题、跑一次 Agent，再把草稿提升成正式报告。"""
    topic, repositories = seed_topic(
        workspace,
        publish_mode=publish_mode,
        repo_count=repo_count,
    )
    agent_run_id = run_agent_once(session, topic.id)
    generated = generate_report(session, agent_run_id=agent_run_id, now=NOW)
    return SeededReport(
        report_id=generated.report_id,
        agent_run_id=agent_run_id,
        topic=topic,
        repositories=repositories,
    )


def seed_published_sibling(session: Session, case: SeededReport) -> Report:
    """插入一份已发布的兄弟报告，让该主题不再是首轮运行。

    它复用这份报告自己的 Agent 运行，从而让模型 / 提示词 / 工具的指纹
    保持完全一致 —— 否则 `MODEL_OR_PROMPT_CHANGED` 会盖住被测行为。
    """
    sibling = Report(
        agent_run_id=case.agent_run_id,
        topic_id=case.topic.id,
        title="上周周报",
        current_version=1,
        content_markdown="# 上周周报\n",
        status="published",
        visibility="INTERNAL",
        sensitivity="INTERNAL",
        publish_mode="AUTO_WITH_DELAY",
        generated_at=NOW,
        published_at=NOW,
    )
    session.add(sibling)
    session.commit()
    return sibling
