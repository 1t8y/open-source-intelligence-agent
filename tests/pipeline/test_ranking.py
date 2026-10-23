"""Tests for deterministic topic candidate ranking."""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.base import Base
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    RepoSnapshot,
    Topic,
    TopicRepository,
)
from open_source_intelligence.pipeline.ranking import rank_topic_candidates


def build_session_factory() -> sessionmaker[Session]:
    """Build a shared in-memory database for ranking tests."""
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
    repository_id: int,
    full_name: str,
    language: str | None,
    description: str,
    topics: list[str],
    first_stars: int,
    latest_stars: int,
    first_forks: int,
    latest_forks: int,
    with_snapshots: bool = True,
) -> Repository:
    """Insert a repository and optional two-point metric history."""
    source = session.scalar(select(DataSource).where(DataSource.source_type == "github"))
    if source is None:
        source = DataSource(
            id=1,
            name="GitHub",
            source_type="github",
            base_url="https://api.github.com",
        )
        session.add(source)
        session.flush()

    repository = Repository(
        id=repository_id,
        source_id=source.id,
        provider_repo_id=f"repo-{repository_id}",
        full_name=full_name,
        html_url=f"https://github.com/{full_name}",
        description=description,
        language=language,
        topics_json=topics,
        stars=latest_stars,
        forks=latest_forks,
        first_seen_at=datetime(2026, 10, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 10, 7, tzinfo=UTC),
    )
    session.add(repository)
    if with_snapshots:
        session.add_all(
            [
                RepoSnapshot(
                    id=repository_id * 10,
                    repository_id=repository_id,
                    snapshot_date=date(2026, 10, 1),
                    stars=first_stars,
                    forks=first_forks,
                ),
                RepoSnapshot(
                    id=repository_id * 10 + 1,
                    repository_id=repository_id,
                    snapshot_date=date(2026, 10, 7),
                    stars=latest_stars,
                    forks=latest_forks,
                ),
            ]
        )
    return repository


def add_topic(session: Session, *, max_candidates: int = 2) -> Topic:
    """Insert one enabled Python/Agent topic."""
    session.add(
        User(
            id=1,
            username="admin",
            password_hash="hash",
            display_name="Admin",
        )
    )
    topic = Topic(
        id=1,
        name="AI Agent",
        languages_json=["Python"],
        keywords_json=["agent"],
        min_stars=100,
        window_days=7,
        max_candidates=max_candidates,
        publish_mode="AUTO_WITH_DELAY",
        enabled=True,
        created_by=1,
    )
    session.add(topic)
    session.commit()
    return topic


def test_ranking_filters_orders_and_marks_agent_candidates() -> None:
    """Topic filters, score ordering, and selection limits must be deterministic."""
    session_factory = build_session_factory()
    with session_factory() as session:
        topic = add_topic(session)
        add_repository(
            session,
            repository_id=1,
            full_name="example/first",
            language="Python",
            description="Agent framework",
            topics=["agent"],
            first_stars=100,
            latest_stars=180,
            first_forks=10,
            latest_forks=20,
        )
        add_repository(
            session,
            repository_id=2,
            full_name="example/second",
            language="Python",
            description="Agent runtime",
            topics=["llm"],
            first_stars=120,
            latest_stars=170,
            first_forks=8,
            latest_forks=18,
        )
        add_repository(
            session,
            repository_id=3,
            full_name="example/third",
            language="Python",
            description="Agent tools",
            topics=["agent"],
            first_stars=300,
            latest_stars=340,
            first_forks=30,
            latest_forks=35,
        )
        add_repository(
            session,
            repository_id=4,
            full_name="example/javascript",
            language="JavaScript",
            description="Agent framework",
            topics=["agent"],
            first_stars=100,
            latest_stars=300,
            first_forks=10,
            latest_forks=50,
        )
        add_repository(
            session,
            repository_id=5,
            full_name="example/unrelated",
            language="Python",
            description="Data processing",
            topics=["pipeline"],
            first_stars=100,
            latest_stars=300,
            first_forks=10,
            latest_forks=50,
        )
        add_repository(
            session,
            repository_id=6,
            full_name="example/small",
            language="Python",
            description="Agent framework",
            topics=["agent"],
            first_stars=20,
            latest_stars=50,
            first_forks=2,
            latest_forks=5,
        )
        add_repository(
            session,
            repository_id=7,
            full_name="example/no-history",
            language="Python",
            description="Agent framework",
            topics=["agent"],
            first_stars=100,
            latest_stars=200,
            first_forks=10,
            latest_forks=20,
            with_snapshots=False,
        )
        session.commit()

        ranked = rank_topic_candidates(session, topic, as_of_date=date(2026, 10, 7))
        rows = session.scalars(select(TopicRepository).order_by(TopicRepository.rank)).all()

    assert [candidate.repository_id for candidate in ranked] == [1, 2, 3]
    assert [candidate.rank for candidate in ranked] == [1, 2, 3]
    assert [candidate.selected_for_agent for candidate in ranked] == [True, True, False]
    assert ranked[0].score == Decimal("100.180000")
    assert [row.repository_id for row in rows] == [1, 2, 3]
    assert [row.selected_for_agent for row in rows] == [True, True, False]


def test_repeated_ranking_replaces_stale_candidates() -> None:
    """A rerun must not duplicate rows and must remove candidates that no longer match."""
    session_factory = build_session_factory()
    with session_factory() as session:
        topic = add_topic(session)
        first = add_repository(
            session,
            repository_id=1,
            full_name="example/first",
            language="Python",
            description="Agent framework",
            topics=["agent"],
            first_stars=100,
            latest_stars=180,
            first_forks=10,
            latest_forks=20,
        )
        stale = add_repository(
            session,
            repository_id=2,
            full_name="example/stale",
            language="Python",
            description="Agent runtime",
            topics=["agent"],
            first_stars=120,
            latest_stars=170,
            first_forks=8,
            latest_forks=18,
        )
        session.commit()

        rank_topic_candidates(session, topic, as_of_date=date(2026, 10, 7))
        rank_topic_candidates(session, topic, as_of_date=date(2026, 10, 7))
        assert len(session.scalars(select(TopicRepository)).all()) == 2

        stale.language = "Go"
        session.commit()
        ranked = rank_topic_candidates(session, topic, as_of_date=date(2026, 10, 7))
        rows = session.scalars(select(TopicRepository)).all()

    assert first.id in {row.repository_id for row in rows}
    assert [candidate.repository_id for candidate in ranked] == [1]
    assert [row.repository_id for row in rows] == [1]


def test_negative_growth_is_clamped_to_nonnegative_score() -> None:
    """Database score constraints must hold when repository growth is negative."""
    session_factory = build_session_factory()
    with session_factory() as session:
        topic = add_topic(session)
        add_repository(
            session,
            repository_id=1,
            full_name="example/declining",
            language="Python",
            description="Agent framework",
            topics=["agent"],
            first_stars=200,
            latest_stars=120,
            first_forks=20,
            latest_forks=15,
        )
        session.commit()

        ranked = rank_topic_candidates(session, topic, as_of_date=date(2026, 10, 7))

    assert len(ranked) == 1
    assert ranked[0].score == Decimal("0.000000")


def test_a_pinned_link_never_shares_a_position() -> None:
    """Keeping a pinned link must not leave it holding another candidate's rank.

    The in-place refresh gives every scored candidate a fresh position and leaves the pinned,
    unscored ones alone — including their old `rank`. When the pinned link happened to hold, say,
    4 and the new pass also produced a 4, two rows shared a position. Everything that orders by
    rank (the candidate list, the report's provenance line, the Agent's candidate loader) then got
    an arbitrary order between them, so two reports over the same data could list the pool
    differently.
    """
    session_factory = build_session_factory()
    with session_factory() as session:
        topic = add_topic(session, max_candidates=2)
        for repository_id, (name, stars) in enumerate(
            (("example/first", 900), ("example/second", 700)), start=1
        ):
            add_repository(
                session,
                repository_id=repository_id,
                full_name=name,
                language="Python",
                description="Agent framework",
                topics=["agent"],
                first_stars=100,
                latest_stars=stars,
                first_forks=10,
                latest_forks=30,
            )
        add_repository(
            session,
            repository_id=3,
            full_name="example/fixture",
            language="Rust",
            description="Boundary fixture",
            topics=["fixture"],
            first_stars=100,
            latest_stars=110,
            first_forks=10,
            latest_forks=11,
            with_snapshots=False,
        )
        # `rank=1` collides with the winner of any pass: a fixture parked at the top of the list.
        session.add(
            TopicRepository(
                topic_id=topic.id,
                repository_id=3,
                score=Decimal("0"),
                rank=1,
                selected_for_agent=False,
                manually_pinned=True,
            )
        )
        session.commit()

        rank_topic_candidates(session, topic, as_of_date=date(2026, 10, 7))
        ranks = session.scalars(
            select(TopicRepository.rank)
            .where(TopicRepository.topic_id == topic.id)
            .order_by(TopicRepository.rank)
        ).all()
        fixture = session.scalar(
            select(TopicRepository).where(
                TopicRepository.topic_id == topic.id, TopicRepository.repository_id == 3
            )
        )

    assert len(ranks) == len(set(ranks)), f"two candidates share a position: {ranks}"
    assert fixture is not None
    # Parked after the scored pool, not promoted over it.
    assert fixture.rank == 3


def test_a_pinned_candidate_survives_a_ranking_pass() -> None:
    """Ranking refreshes the pool; it does not get to delete a link a human pinned.

    An evaluation topic holds exactly one candidate chosen for the shape it exercises, and that
    candidate usually does not win the star/fork ranking. Rebuilding the list from scratch dropped
    it, so one daily pass over a curated topic silently emptied the case it exists to measure.
    """
    session_factory = build_session_factory()
    with session_factory() as session:
        topic = add_topic(session, max_candidates=1)
        add_repository(
            session,
            repository_id=1,
            full_name="example/first",
            language="Python",
            description="Agent framework",
            topics=["agent"],
            first_stars=100,
            latest_stars=500,
            first_forks=10,
            latest_forks=40,
        )
        add_repository(
            session,
            repository_id=2,
            full_name="example/second",
            language="Python",
            description="Another agent framework",
            topics=["agent"],
            first_stars=100,
            latest_stars=200,
            first_forks=10,
            latest_forks=20,
        )
        # A curated, non-matching repository: it can never win the ranking, which is exactly why
        # the pin has to protect it.
        add_repository(
            session,
            repository_id=3,
            full_name="example/fixture",
            language="Rust",
            description="Boundary fixture",
            topics=["fixture"],
            first_stars=100,
            latest_stars=110,
            first_forks=10,
            latest_forks=11,
        )
        session.add(
            TopicRepository(
                topic_id=topic.id,
                repository_id=3,
                score=Decimal("0"),
                rank=99,
                selected_for_agent=True,
                manually_pinned=True,
            )
        )
        # A stale, unpinned link for a repository the ranker will not select.
        session.add(
            TopicRepository(
                topic_id=topic.id,
                repository_id=2,
                score=Decimal("0"),
                rank=50,
                selected_for_agent=True,
                manually_pinned=False,
            )
        )
        session.commit()

        rank_topic_candidates(session, topic, as_of_date=date(2026, 10, 7))

        links = {
            link.repository_id: link
            for link in session.scalars(
                select(TopicRepository).where(TopicRepository.topic_id == topic.id)
            ).all()
        }
        assert links[3].selected_for_agent is True
        assert links[3].manually_pinned is True
        assert links[1].selected_for_agent is True
        # Repo 2 was ranked and lost the cut-off, so its automatic selection is refreshed to False
        # and the link survives as a scored-but-unselected candidate.
        assert links[2].selected_for_agent is False
