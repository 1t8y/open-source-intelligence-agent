"""Tests for idempotent daily repository snapshots."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.base import Base
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    RepoSnapshot,
)
from open_source_intelligence.pipeline.snapshots import (
    create_daily_snapshots,
    get_snapshot_series,
)


def build_session_factory() -> sessionmaker[Session]:
    """Build a shared in-memory database for pipeline tests."""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def add_repository(
    session: Session,
    *,
    stars: int = 100,
    forks: int = 10,
    open_issues: int | None = 8,
) -> Repository:
    """Insert one repository with deterministic counters."""
    source = DataSource(
        name="GitHub",
        source_type="github",
        base_url="https://api.github.com",
    )
    repository = Repository(
        source=source,
        provider_repo_id="repo-1",
        full_name="example/agent",
        html_url="https://github.com/example/agent",
        language="Python",
        stars=stars,
        forks=forks,
        open_issues=open_issues,
        first_seen_at=datetime(2026, 10, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    session.add(repository)
    session.commit()
    return repository


def test_snapshot_creation_is_idempotent_and_computes_daily_deltas() -> None:
    """Repeated runs update one row per day and calculate deltas from history."""
    session_factory = build_session_factory()
    first_day = date(2026, 10, 1)
    second_day = first_day + timedelta(days=1)

    with session_factory() as session:
        repository = add_repository(session)

        assert create_daily_snapshots(session, snapshot_date=first_day) == 1
        first_snapshot = session.scalar(select(RepoSnapshot))
        assert first_snapshot is not None
        assert first_snapshot.stars == 100
        assert first_snapshot.forks == 10
        assert first_snapshot.open_issues == 8
        assert first_snapshot.stars_delta is None
        assert first_snapshot.forks_delta is None

        repository.stars = 105
        repository.forks = 12
        assert create_daily_snapshots(session, snapshot_date=first_day) == 1
        snapshots = session.scalars(select(RepoSnapshot)).all()
        assert len(snapshots) == 1
        assert snapshots[0].stars == 105
        assert snapshots[0].forks == 12
        assert snapshots[0].stars_delta is None
        assert snapshots[0].forks_delta is None

        repository.stars = 130
        repository.forks = 18
        assert create_daily_snapshots(session, snapshot_date=second_day) == 1
        assert create_daily_snapshots(session, snapshot_date=second_day) == 1

        series = get_snapshot_series(
            session,
            repository.id,
            start_date=first_day,
            end_date=second_day,
        )

    assert len(series) == 2
    assert [snapshot.snapshot_date for snapshot in series] == [first_day, second_day]
    assert series[1].stars_delta == 25
    assert series[1].forks_delta == 6
