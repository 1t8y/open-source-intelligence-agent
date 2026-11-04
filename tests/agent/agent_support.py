"""Agent 运行时测试共用的种子数据辅助。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from support import Workspace

from open_source_intelligence.persistence.models.intelligence import Repository, Topic

FIRST_SNAPSHOT_DATE = date(2026, 10, 5)
LAST_SNAPSHOT_DATE = date(2026, 10, 11)


@dataclass(slots=True)
class SeededTopic:
    """一个主题及其按排名排列的候选仓库。"""

    topic: Topic
    repositories: list[Repository]


def seed_topic(workspace: Workspace, *, repo_count: int = 2) -> SeededTopic:
    """构造一个主题，附带候选排名、指标序列与缓存的 README 内容。"""
    topic = workspace.add_topic()
    source = workspace.add_source()
    repositories: list[Repository] = []
    for index in range(repo_count):
        repository = workspace.add_repository(
            source_id=source.id,
            provider_repo_id=str(index + 1),
            full_name=f"acme/repo-{index + 1}",
            stars=100 + index,
        )
        workspace.link(topic.id, repository.id, rank=index + 1)
        workspace.add_snapshot(
            repository.id,
            FIRST_SNAPSHOT_DATE,
            stars=100 + index,
            forks=10,
        )
        workspace.add_snapshot(
            repository.id,
            LAST_SNAPSHOT_DATE,
            stars=140 + index,
            forks=12,
        )
        workspace.add_content(
            repository.id,
            content="# README\nA cached repository readme used by the research Agent.",
        )
        repositories.append(repository)
    return SeededTopic(topic=topic, repositories=repositories)
