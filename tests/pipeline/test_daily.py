"""Tests for the daily collect, snapshot, rank orchestration."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from open_source_intelligence.collectors.types import (
    CollectedRepository,
    FetchedContent,
    SourceUnavailableError,
)
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.base import Base
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import (
    PipelineRun,
    Repository,
    RepositoryContent,
    RepoSnapshot,
    Topic,
    TopicRepository,
)
from open_source_intelligence.pipeline import daily
from open_source_intelligence.pipeline.daily import (
    PipelineStageError,
    run_daily_pipeline,
)

BUSINESS_DATE = date(2026, 10, 12)
OBSERVED_AT = datetime(2026, 10, 12, 1, 0, tzinfo=UTC)


def build_session_factory() -> sessionmaker[Session]:
    """Build a shared in-memory database for orchestration tests."""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def build_item(index: int, *, stars: int = 100, provider: str = "github") -> CollectedRepository:
    """Build one normalized repository payload for a stub collector."""
    return CollectedRepository(
        provider=provider,
        provider_repo_id=f"repo-{index}",
        full_name=f"example/repo-{index}",
        html_url=f"https://github.com/example/repo-{index}",
        description="Agent framework",
        language="Python",
        license="MIT",
        topics=("agent",),
        stars=stars,
        forks=10,
        observed_at=OBSERVED_AT,
    )


class StubCollector:
    """Collector double that returns fixed payloads or raises a source error."""

    def __init__(
        self,
        items: list[CollectedRepository],
        error: Exception | None = None,
        *,
        provider: str = "github",
        display_name: str = "GitHub",
        base_url: str = "https://api.github.com",
    ) -> None:
        self._items = items
        self._error = error
        self.provider = provider
        self.display_name = display_name
        self.base_url = base_url

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        if self._error is not None:
            raise self._error
        return self._items[:limit]


def add_topic(session: Session, *, name: str, enabled: bool = True) -> Topic:
    """Insert one topic owned by a test user."""
    user = session.scalar(select(User).where(User.id == 1))
    if user is None:
        session.add(
            User(
                id=1,
                username="admin",
                password_hash="hash",
                display_name="Admin",
            )
        )
        session.flush()
    topic = Topic(
        name=name,
        languages_json=None,
        keywords_json=None,
        min_stars=0,
        window_days=7,
        max_candidates=10,
        publish_mode="AUTO_WITH_DELAY",
        enabled=enabled,
        created_by=1,
    )
    session.add(topic)
    session.commit()
    return topic


def test_daily_pipeline_records_every_stage_and_stays_idempotent() -> None:
    """Each stage gets a run record, and a rerun must not duplicate business rows."""
    session_factory = build_session_factory()
    collector = StubCollector([build_item(1), build_item(2, stars=500)])
    with session_factory() as session:
        topic = add_topic(session, name="AI Agent")
        add_topic(session, name="Disabled topic", enabled=False)

        first = run_daily_pipeline(
            session,
            business_date=BUSINESS_DATE,
            collectors=[collector],
            limit=5,
        )
        second = run_daily_pipeline(
            session,
            business_date=BUSINESS_DATE,
            collectors=[collector],
            limit=5,
            attempt=2,
        )

        run_types = [stage.run_type for stage in first.stages]
        run_row_count = session.scalar(select(func.count(PipelineRun.id)))
        snapshot_count = session.scalar(select(func.count(RepoSnapshot.id)))
        candidate_count = session.scalar(select(func.count(TopicRepository.topic_id)))
        repository_count = session.scalar(select(func.count(Repository.id)))
        second_attempts = sorted({stage.run_id for stage in second.stages})
        first_attempt = session.scalar(
            select(PipelineRun.attempt).where(PipelineRun.id == first.stages[0].run_id)
        )

    assert run_types == ["collect", "snapshot", "rank"]
    assert first.status == "succeeded"
    assert first.provider_counts == {"github": 2}
    assert first.provider_errors == {}
    assert first.snapshot_count == 2
    assert first.topic_candidate_counts == {str(topic.id): 2}
    assert repository_count == 2
    assert snapshot_count == 2
    assert candidate_count == 2
    assert first_attempt == 1
    assert len(second_attempts) == 3
    # 6 run rows: two attempts of collect + snapshot + rank.
    assert run_row_count == 6


def test_all_provider_failure_fails_the_collect_run_and_stops_the_batch() -> None:
    """A batch where no provider returns data must fail instead of ranking nothing."""
    session_factory = build_session_factory()
    collector = StubCollector([], error=SourceUnavailableError("source returned HTTP 500"))
    with session_factory() as session:
        add_topic(session, name="AI Agent")

        with pytest.raises(PipelineStageError) as excinfo:
            run_daily_pipeline(
                session,
                business_date=BUSINESS_DATE,
                collectors=[collector],
                limit=5,
            )

        runs = session.scalars(select(PipelineRun).order_by(PipelineRun.id)).all()
        snapshot_runs = session.scalar(
            select(func.count(PipelineRun.id)).where(PipelineRun.run_type == "snapshot")
        )

    assert excinfo.value.error_code == "COLLECTOR_FAILED"
    assert excinfo.value.run_type == "collect"
    assert [run.run_type for run in runs] == ["collect"]
    assert runs[0].status == "failed"
    assert runs[0].error_code == "COLLECTOR_FAILED"
    assert "HTTP 500" in (runs[0].error_message or "")
    assert snapshot_runs == 0


def test_partial_provider_failure_continues_the_batch() -> None:
    """One failing provider must not stop snapshots and ranking."""
    session_factory = build_session_factory()
    working = StubCollector([build_item(1)])
    broken = StubCollector(
        [],
        error=SourceUnavailableError("source timed out"),
        provider="huggingface",
        display_name="Hugging Face",
        base_url="https://hf-mirror.com/api",
    )
    with session_factory() as session:
        add_topic(session, name="AI Agent")

        result = run_daily_pipeline(
            session,
            business_date=BUSINESS_DATE,
            collectors=[working, broken],
            limit=5,
        )

    assert result.status == "partial"
    assert result.provider_counts == {"github": 1, "huggingface": 0}
    assert result.provider_errors == {"huggingface": "source timed out"}
    assert [stage.run_type for stage in result.stages] == ["collect", "snapshot", "rank"]
    assert result.stages[0].status == "partial"
    assert result.stages[1].status == "succeeded"
    assert result.stages[2].status == "succeeded"


def test_rank_failure_is_isolated_to_one_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One broken topic must not stop the ranking of the other topics."""
    session_factory = build_session_factory()
    collector = StubCollector([build_item(1)])

    with session_factory() as session:
        broken_topic = add_topic(session, name="Broken topic")
        healthy_topic = add_topic(session, name="Healthy topic")
        original = daily.rank_topic_candidates

        def flaky_rank(session: Session, topic: Topic, *, as_of_date: date) -> list[object]:
            if topic.id == broken_topic.id:
                raise RuntimeError("ranking exploded")
            return original(session, topic, as_of_date=as_of_date)

        monkeypatch.setattr(daily, "rank_topic_candidates", flaky_rank)
        result = run_daily_pipeline(
            session,
            business_date=BUSINESS_DATE,
            collectors=[collector],
            limit=5,
        )

        rank_runs = session.scalars(
            select(PipelineRun).where(PipelineRun.run_type == "rank").order_by(PipelineRun.id)
        ).all()

    assert result.status == "partial"
    assert [stage.status for stage in result.stages] == [
        "succeeded",
        "succeeded",
        "failed",
        "succeeded",
    ]
    assert result.topic_candidate_counts == {str(healthy_topic.id): 1}
    assert [run.topic_id for run in rank_runs] == [broken_topic.id, healthy_topic.id]
    assert rank_runs[0].status == "failed"
    assert rank_runs[0].error_code == "RANK_FAILED"
    assert rank_runs[1].status == "succeeded"


def test_attempt_is_recorded_on_every_stage_for_the_retry_trail() -> None:
    """Retried attempts must be visible on each run record of the attempt."""
    session_factory = build_session_factory()
    collector = StubCollector([build_item(1)])
    with session_factory() as session:
        add_topic(session, name="AI Agent")

        result = run_daily_pipeline(
            session,
            business_date=BUSINESS_DATE,
            collectors=[collector],
            limit=5,
            attempt=3,
        )

        attempts = session.scalars(
            select(PipelineRun.attempt).where(
                PipelineRun.id.in_([stage.run_id for stage in result.stages])
            )
        ).all()

    assert result.attempt == 3
    assert list(attempts) == [3, 3, 3]


class StubContentSource:
    """Content source double for the orchestration tests."""

    provider = "github"

    def __init__(self, text: str = "# README") -> None:
        self._text = text
        self.calls: list[str] = []

    def fetch_readme(self, full_name: str) -> FetchedContent:
        """Return one README and record the name that was asked for."""
        self.calls.append(full_name)
        return FetchedContent(
            provider=self.provider,
            full_name=full_name,
            content_type="readme",
            source_url=f"https://github.com/{full_name}/blob/main/README.md",
            content=self._text,
            fetched_at=OBSERVED_AT,
        )


def test_the_contents_stage_is_opt_in_and_caches_the_ranked_candidates() -> None:
    """The batch stays three stages until a source is supplied, and then it warms the cache."""
    session_factory = build_session_factory()
    collector = StubCollector([build_item(1), build_item(2, stars=500)])
    source = StubContentSource()
    with session_factory() as session:
        add_topic(session, name="AI Agent")

        without = run_daily_pipeline(
            session,
            business_date=BUSINESS_DATE,
            collectors=[collector],
            limit=5,
        )
        with_content = run_daily_pipeline(
            session,
            business_date=BUSINESS_DATE,
            collectors=[collector],
            limit=5,
            content_sources=[source],
            content_limit=1,
        )

        cached = session.scalars(select(RepositoryContent)).all()

    assert [stage.run_type for stage in without.stages] == ["collect", "snapshot", "rank"]
    assert without.content_counts == {}
    assert [stage.run_type for stage in with_content.stages] == [
        "collect",
        "snapshot",
        "rank",
        "contents",
    ]
    assert with_content.content_counts["candidate_count"] == 1
    assert with_content.content_counts["fetched"] == 1
    assert len(source.calls) == 1
    assert len(cached) == 1
    assert cached[0].content == "# README"
