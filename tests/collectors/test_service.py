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


def test_collection_adopts_a_row_keyed_on_its_name() -> None:
    """A repository already on file under its name must not gain a second row.

    The demo seeder writes GitHub rows keyed on `owner/name`, because it never sees the numeric
    provider id. When the collector later meets the same repository it carries that id, the unique
    key `(source_id, provider_repo_id)` does not fire — the ids differ — and the insert used to
    create a second row for one project: two star counts, two snapshot histories, and a topic that
    ranks the same repository twice. The collector has to adopt the existing row instead.
    """
    session_factory = build_session_factory()
    with session_factory() as session:
        collect_repositories(session, [FakeCollector(stars=10)], limit=1)
        seeded = session.query(Repository).one()
        # Rewrite it into the shape the seeder leaves behind: name-shaped id, stale counters.
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
    """Provider failures should not abort other sources and must be audited."""
    session_factory = build_session_factory()
    with session_factory() as session:
        summary = collect_repositories(session, [FakeCollector(stars=0, fail=True)], limit=1)
        event = session.query(AuditLog).one()

    assert summary.provider_counts == {"github": 0}
    assert summary.errors == {"github": "provider unavailable"}
    assert event.result == "failed"
