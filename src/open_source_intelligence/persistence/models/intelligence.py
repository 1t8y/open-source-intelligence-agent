"""Source, topic, repository, snapshot, and pipeline models."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import (
    Base,
    TimestampMixin,
    UTCDateTime,
    uint64_pk,
    unsigned_bigint,
    utc_now,
)
from .identity import User


class DataSource(Base, TimestampMixin):
    """External source such as GitHub or Hugging Face."""

    __tablename__ = "data_sources"
    __table_args__ = (
        CheckConstraint("rate_limit_per_minute > 0", name="rate_limit_positive"),
        UniqueConstraint("source_type"),
    )

    id: Mapped[uint64_pk]
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    repositories: Mapped[list[Repository]] = relationship(back_populates="source")


class Topic(Base, TimestampMixin):
    """Monitoring topic and candidate-selection policy."""

    __tablename__ = "topics"
    __table_args__ = (
        CheckConstraint("min_stars >= 0", name="min_stars_nonnegative"),
        CheckConstraint("window_days > 0", name="window_days_positive"),
        CheckConstraint("max_candidates > 0", name="max_candidates_positive"),
        CheckConstraint(
            "publish_mode IN ('AUTO', 'AUTO_WITH_DELAY', 'MANUAL')",
            name="publish_mode_valid",
        ),
    )

    id: Mapped[uint64_pk]
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    languages_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    keywords_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    min_stars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    max_candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    publish_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    creator: Mapped[User] = relationship()
    repository_links: Mapped[list[TopicRepository]] = relationship(
        back_populates="topic",
        cascade="all, delete-orphan",
    )
    pipeline_runs: Mapped[list[PipelineRun]] = relationship(back_populates="topic")


class Repository(Base):
    """Normalized repository identity and latest counters."""

    __tablename__ = "repositories"
    __table_args__ = (
        CheckConstraint("stars >= 0", name="stars_nonnegative"),
        CheckConstraint("forks >= 0", name="forks_nonnegative"),
        UniqueConstraint("source_id", "provider_repo_id"),
        Index("ix_repositories_language_stars", "language", "stars"),
        Index("ix_repositories_last_seen", "last_seen_at"),
    )

    id: Mapped[uint64_pk]
    source_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("data_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_repo_id: Mapped[str] = mapped_column(String(128), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    html_url: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    license: Mapped[str | None] = mapped_column(String(128), nullable=True)
    topics_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    stars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    forks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    source: Mapped[DataSource] = relationship(back_populates="repositories")
    snapshots: Mapped[list[RepoSnapshot]] = relationship(
        back_populates="repository",
        cascade="all, delete-orphan",
    )
    topic_links: Mapped[list[TopicRepository]] = relationship(
        back_populates="repository",
        cascade="all, delete-orphan",
    )


class RepoSnapshot(Base):
    """Daily point-in-time repository counters."""

    __tablename__ = "repo_snapshots"
    __table_args__ = (
        CheckConstraint("stars >= 0", name="stars_nonnegative"),
        CheckConstraint("forks >= 0", name="forks_nonnegative"),
        CheckConstraint("open_issues IS NULL OR open_issues >= 0", name="open_issues_nonnegative"),
        UniqueConstraint("repository_id", "snapshot_date"),
        Index("ix_repo_snapshots_date_stars_delta", "snapshot_date", "stars_delta"),
    )

    id: Mapped[uint64_pk]
    repository_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    stars: Mapped[int] = mapped_column(Integer, nullable=False)
    forks: Mapped[int] = mapped_column(Integer, nullable=False)
    open_issues: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stars_delta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forks_delta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )

    repository: Mapped[Repository] = relationship(back_populates="snapshots")


class TopicRepository(Base):
    """Candidate score and Agent selection state for a topic."""

    __tablename__ = "topic_repositories"
    __table_args__ = (
        CheckConstraint("score >= 0", name="score_nonnegative"),
        CheckConstraint("rank >= 1", name="rank_positive"),
        Index("ix_topic_repositories_topic_rank", "topic_id", "rank"),
    )

    topic_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("topics.id", ondelete="CASCADE"),
        primary_key=True,
    )
    repository_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    score: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_for_agent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )

    topic: Mapped[Topic] = relationship(back_populates="repository_links")
    repository: Mapped[Repository] = relationship(back_populates="topic_links")


class PipelineRun(Base):
    """Execution record for collection, snapshot, ranking, and Agent pipelines."""

    __tablename__ = "pipeline_runs"
    __table_args__ = (
        CheckConstraint(
            "run_type IN ('collect', 'snapshot', 'rank', 'agent', 'publish')",
            name="run_type_valid",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'partial')",
            name="status_valid",
        ),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name="time_order_valid",
        ),
        CheckConstraint(
            "run_type IN ('collect', 'snapshot', 'rank') OR topic_id IS NOT NULL",
            name="topic_scope_valid",
        ),
        Index("ix_pipeline_runs_topic_type_status", "topic_id", "run_type", "status"),
        Index("ix_pipeline_runs_started_at", "started_at"),
    )

    id: Mapped[uint64_pk]
    topic_id: Mapped[int | None] = mapped_column(
        unsigned_bigint(),
        ForeignKey("topics.id", ondelete="RESTRICT"),
        nullable=True,
    )
    run_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    metrics_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    topic: Mapped[Topic | None] = relationship(back_populates="pipeline_runs")
