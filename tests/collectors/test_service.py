"""守护采集入库的幂等性、行接管与审计留痕。"""

from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from open_source_intelligence.collectors.service import collect_repositories
from open_source_intelligence.collectors.types import (
    CollectedRepository,
    SourceUnavailableError,
)
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.base import Base
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.intelligence import Repository


class FakeCollector:
    """返回确定性归一化仓库的测试用采集器。"""

    provider = "github"
    display_name = "GitHub"
    base_url = "https://api.github.com"

    def __init__(self, stars: int, *, fail: bool = False) -> None:
        self.stars = stars
        self.fail = fail

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        if self.fail:
            raise SourceUnavailableError("provider unavailable")
        return [
            CollectedRepository(
                provider=self.provider,
                provider_repo_id=f"repo-{index}",
                full_name=f"example/repo-{index}",
                html_url=f"https://github.com/example/repo-{index}",
                description="test",
                language="Python",
                license="MIT",
                topics=("agent",),
                stars=self.stars,
                forks=2,
                open_issues=7,
                observed_at=datetime(2026, 10, 9, tzinfo=UTC),
            )
            for index in range(limit)
        ]


def build_session_factory() -> sessionmaker[Session]:
    """构造共享的内存 SQLite 会话工厂，两次采集才会落在同一个库上。"""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_repeated_collection_is_idempotent_and_updates_metrics() -> None:
    """重复采集应原地更新已有仓库，指标取最新值而不是再插一行。"""
    session_factory = build_session_factory()
    with session_factory() as session:
        collect_repositories(session, [FakeCollector(stars=10)], limit=2)
        collect_repositories(session, [FakeCollector(stars=20)], limit=2)

        repositories = session.query(Repository).order_by(Repository.id).all()
        audits = session.query(AuditLog).all()

    assert len(repositories) == 2
    assert {repository.stars for repository in repositories} == {20}
    assert {repository.open_issues for repository in repositories} == {7}
    assert len(audits) == 2
    assert all(event.action == "collector.run" for event in audits)


def test_collection_adopts_a_row_keyed_on_its_name() -> None:
    """已按名称入库的仓库，不能因为标识键不同就多出一行。

    演示数据播种器只能按 `owner/name` 写 GitHub 行，因为它拿不到数字形式的
    供应商 id。采集器随后遇到同一个仓库时带着那个 id，唯一键
    `(source_id, provider_repo_id)` 因两个 id 不同而不会命中，插入语句便会
    给一个项目留下两行：两套 star 数、两段快照历史，以及一个把同一仓库排两次
    的主题。采集器必须改为接管已有的那一行。
    """
    session_factory = build_session_factory()
    with session_factory() as session:
        collect_repositories(session, [FakeCollector(stars=10)], limit=1)
        seeded = session.query(Repository).one()
        # 把它改写成播种器留下的形状：名称形式的 id、过期的计数。
        seeded.provider_repo_id = seeded.full_name
        seeded.stars = 1
        session.commit()

        collect_repositories(session, [FakeCollector(stars=20)], limit=1)

        repositories = session.query(Repository).all()

    assert len(repositories) == 1, "the collector inserted a second row for one repository"
    adopted = repositories[0]
    assert adopted.id == seeded.id
    assert adopted.provider_repo_id == "repo-0"
    assert adopted.stars == 20


def test_collector_failure_is_audited_and_summarized() -> None:
    """单个来源失败不该中断其它来源，而且必须留下审计记录。"""
    session_factory = build_session_factory()
    with session_factory() as session:
        summary = collect_repositories(session, [FakeCollector(stars=0, fail=True)], limit=1)
        event = session.query(AuditLog).one()

    assert summary.provider_counts == {"github": 0}
    assert summary.errors == {"github": "provider unavailable"}
    assert event.result == "failed"
