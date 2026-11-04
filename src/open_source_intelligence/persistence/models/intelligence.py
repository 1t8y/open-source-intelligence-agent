"""数据源、主题、仓库、快照、流水线与回填任务的模型。"""

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
    """外部数据源，例如 GitHub 或 Hugging Face。"""

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


#: `topics.publish_mode` 允许的取值，与下面的 `publish_mode_valid` 约束一一对应。
#: 单独命名的理由与 `REPORT_PUBLISH_MODES` 相同：界面必须只提供数据库真正接受的取值，而在
#: 模板里重新敲一遍的列表迟早会走样。
TOPIC_PUBLISH_MODES: tuple[str, ...] = ("AUTO", "AUTO_WITH_DELAY", "MANUAL")


class Topic(Base, TimestampMixin):
    """监控主题与候选筛选策略。"""

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
    """规范化后的仓库身份与最新计数。"""

    __tablename__ = "repositories"
    __table_args__ = (
        CheckConstraint("stars >= 0", name="stars_nonnegative"),
        CheckConstraint("forks >= 0", name="forks_nonnegative"),
        CheckConstraint("open_issues IS NULL OR open_issues >= 0", name="open_issues_nonnegative"),
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
    open_issues: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    """仓库计数的每日时点快照。"""

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
    """缓存并清洗后的仓库长文本内容，例如 README。"""

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
    """某个主题下候选仓库的得分与 Agent 选中状态。"""

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
    manually_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )

    topic: Mapped[Topic] = relationship(back_populates="repository_links")
    repository: Mapped[Repository] = relationship(back_populates="topic_links")


class PipelineRun(Base):
    """采集、快照、排序与 Agent 流水线的执行记录。"""

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
    """对一个供应商分页搜索结果的可续爬、可恢复遍历。

    一行是一个*任务*，而不是一次尝试。游标（`next_page`）与两个计数器都放在这一行上，
    并且每翻完一页就提交，因此丢失进程的遍历会从它尚未完成的那一页继续，而不是把整段
    历史重走一遍。重放一页本来也无害 —— 入库对 `(source, provider_repo_id)` 幂等 ——
    但连着重放二十页，正是把一次网络抖动变成一次重复抓取的原因。

    `target_pages` 是运维给出的预算，而不是承诺：如果搜索结果先一步耗尽，任务会以
    `succeeded` 关闭，此时 `pages_completed` 低于目标值。`per_page` 上限为 100，因为
    这是两家供应商都遵守的最大分页大小。

    `heartbeat_at` 单独占一列，而不是复用 `updated_at`。`updated_at` 是本 mixin 的
    「这一行变了」时间戳：它的 `onupdate` 由 SQLAlchemy 在每次 UPDATE 时写入，也无法
    用显式赋值钉住，因此今后对这一行的任何写入 —— 迁移回填、运维手工编辑 —— 都会刷新
    它，让一次已被放弃的遍历看起来还活着。专用列只说一件事，而且只在遍历自己说的
    时候才说。
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
