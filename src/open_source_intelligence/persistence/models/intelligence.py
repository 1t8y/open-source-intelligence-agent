"""Source, topic, repository, snapshot, pipeline, and backfill models."""

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
    medium_text,
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


#: The values `topics.publish_mode` accepts, mirroring the `publish_mode_valid` check below.
#: Named for the same reason `REPORT_PUBLISH_MODES` is: the UI has to offer exactly the values the
#: database will take, and a list retyped in the template is a list that drifts.
TOPIC_PUBLISH_MODES: tuple[str, ...] = ("AUTO", "AUTO_WITH_DELAY", "MANUAL")


class Topic(Base, TimestampMixin):
    """Monitoring topic and candidate-selection policy."""

    __tablename__ = "topics"
    __table_args__ = (
        CheckConstraint("min_stars >= 0", name="min_stars_nonnegative"),
        CheckConstraint("window_days > 0", name="window_days_positive"),
        CheckConstraint("max_candidates > 0", name="max_candidates_positive"),
        CheckConstraint(
            f"publish_mode IN ({', '.join(repr(mode) for mode in TOPIC_PUBLISH_MODES)})",
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


class RepositoryContent(Base, TimestampMixin):
    """Cached, cleaned long-form repository content such as the README."""

    __tablename__ = "repository_contents"
    __table_args__ = (
        CheckConstraint("content_type IN ('readme')", name="content_type_valid"),
        UniqueConstraint("repository_id", "content_type"),
    )

    id: Mapped[uint64_pk]
    repository_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
    )
    content_type: Mapped[str] = mapped_column(String(32), nullable=False, default="readme")
    source_url: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(medium_text(), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )

    repository: Mapped[Repository] = relationship()


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
            "run_type IN ('collect', 'snapshot', 'rank', 'contents', 'backfill', "
            "'agent', 'publish', 'push')",
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
            "run_type IN ('collect', 'snapshot', 'rank', 'contents', 'backfill') "
            "OR topic_id IS NOT NULL",
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


class BackfillJob(Base, TimestampMixin):
    """Durable, resumable walk over a provider's paged search results.

    One row is one *job*, not one attempt. The cursor (`next_page`) and the two
    counters live on the row and are committed after every page, so a walk that
    loses its process resumes at the page it had not finished instead of starting
    the whole history again. Replaying a page would be harmless anyway - ingestion
    is idempotent on (source, provider_repo_id) - but replaying twenty of them is
    what turns a network blip into a duplicate crawl.

    `target_pages` is the operator's budget, not a promise: a search that runs out
    of results first closes the job as `succeeded` with `pages_completed` below the
    target. `per_page` is capped at 100 because that is the largest page both
    providers honour.

    `heartbeat_at` is its own column rather than a reuse of `updated_at`. `updated_at`
    is the mixin's "this row changed" stamp: its `onupdate` is applied by SQLAlchemy on
    every UPDATE and cannot be pinned by an explicit assignment, so any future write to
    this row - a migration backfill, an operator edit - would refresh it and make an
    abandoned walk look alive. A dedicated column says exactly one thing and says it only
    when the walk itself says it.
    """

    __tablename__ = "backfill_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint("next_page >= 1", name="next_page_positive"),
        CheckConstraint("target_pages >= 1", name="target_pages_positive"),
        CheckConstraint("per_page >= 1 AND per_page <= 100", name="per_page_bounded"),
        CheckConstraint("pages_completed >= 0", name="pages_completed_nonnegative"),
        CheckConstraint("items_collected >= 0", name="items_collected_nonnegative"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name="time_order_valid",
        ),
        Index("ix_backfill_jobs_provider_status", "provider", "status"),
        Index("ix_backfill_jobs_created_at", "created_at"),
    )

    id: Mapped[uint64_pk]
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    query_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    next_page: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    target_pages: Mapped[int] = mapped_column(Integer, nullable=False)
    per_page: Mapped[int] = mapped_column(Integer, nullable=False)
    pages_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_collected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
