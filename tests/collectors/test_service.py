"""Tests for idempotent collector ingestion and audit logging."""

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
    """Test collector returning deterministic normalized repositories."""

    provider = "github"
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
                observed_at=datetime(2026, 10, 9, tzinfo=UTC),
            )
            for index in range(limit)
        ]


def build_session_factory() -> sessionmaker[Session]:
    """Build a shared in-memory SQLite session factory."""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_repeated_collection_is_idempotent_and_updates_metrics() -> None:
    """Repeated collection should update existing repositories in place."""
    session_factory = build_session_factory()
    with session_factory() as session:
        collect_repositories(session, [FakeCollector(stars=10)], limit=2)
        collect_repositories(session, [FakeCollector(stars=20)], limit=2)

        repositories = session.query(Repository).order_by(Repository.id).all()
        audits = session.query(AuditLog).all()

    assert len(repositories) == 2
    assert {repository.stars for repository in repositories} == {20}
    assert len(audits) == 2
    assert all(event.action == "collector.run" for event in audits)


def test_collector_failure_is_audited_and_summarized() -> None:
    """Provider failures should not abort other sources and must be audited."""
    session_factory = build_session_factory()
    with session_factory() as session:
        summary = collect_repositories(session, [FakeCollector(stars=0, fail=True)], limit=1)
        event = session.query(AuditLog).one()

    assert summary.provider_counts == {"github": 0}
    assert summary.errors == {"github": "provider unavailable"}
    assert event.result == "failed"
