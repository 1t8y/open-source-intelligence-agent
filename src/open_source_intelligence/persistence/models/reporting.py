"""报告、草稿、章节与引用的持久化模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import (
    Base,
    TimestampMixin,
    UTCDateTime,
    medium_text,
    uint64_pk,
    unsigned_bigint,
    utc_now,
)

REPORT_STATUSES = (
    "draft",
    "pending_review",
    "approved",
    "scheduled",
    "published",
    "failed",
    "changes_requested",
)
REPORT_VISIBILITIES = ("PRIVATE", "TEAM", "INTERNAL")
REPORT_SENSITIVITIES = ("PUBLIC", "INTERNAL", "CONFIDENTIAL")
REPORT_PUBLISH_MODES = ("AUTO", "AUTO_WITH_DELAY", "MANUAL")
REPORT_SOURCE_TYPES = ("metric", "readme", "prior_report")
REPORT_DRAFT_STATUSES = ("draft", "superseded", "promoted")
REPORT_VERIFICATION_VERDICTS = ("passed", "failed")
REPORT_REVIEW_DECISIONS = ("approve", "reject", "request_changes")


class Report(Base):
    """由某次 Agent 运行生成、已发布或处于审核中的周报。

    `owner_id` 是对这份报告负责的编辑。它是 `PRIVATE` 可见性唯一的抓手：没有它，
    私密报告会被每一个已认证用户读到，因此正是这一列让最窄的可见范围有了意义。
    """

    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'pending_review', 'approved', 'scheduled', "
            "'published', 'failed', 'changes_requested')",
            name="status_valid",
        ),
        CheckConstraint("visibility IN ('PRIVATE', 'TEAM', 'INTERNAL')", name="visibility_valid"),
        CheckConstraint(
            "sensitivity IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL')",
            name="sensitivity_valid",
        ),
        CheckConstraint(
            "publish_mode IN ('AUTO', 'AUTO_WITH_DELAY', 'MANUAL')",
            name="publish_mode_valid",
        ),
        CheckConstraint("current_version >= 1", name="current_version_positive"),
        Index("ix_reports_topic_status", "topic_id", "status"),
        Index("ix_reports_publish_at", "publish_at"),
        Index("ix_reports_visibility_sensitivity", "visibility", "sensitivity"),
        Index("ix_reports_owner_status", "owner_id", "status"),
    )

    id: Mapped[uint64_pk]
    agent_run_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("agent_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    topic_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("topics.id", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_id: Mapped[int | None] = mapped_column(
        unsigned_bigint(),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_markdown: Mapped[str] = mapped_column(medium_text(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="TEAM")
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False, default="INTERNAL")
    publish_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="AUTO_WITH_DELAY")
    publish_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    approved_by: Mapped[int | None] = mapped_column(unsigned_bigint(), nullable=True)


class ReportVersion(Base):
    """报告在某个已提升版本上的不可变快照。"""

    __tablename__ = "report_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("report_id", "version"),
        Index("ix_report_versions_report_created", "report_id", "created_at"),
    )

    id: Mapped[uint64_pk]
    report_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("reports.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_markdown: Mapped[str] = mapped_column(medium_text(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        unsigned_bigint(),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    change_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )


class ReportSource(Base):
    """支撑报告某个章节的一条可追溯证据引用。

    这些行以所属的报告版本为键，而不只是以报告为键，因为 `report_versions` 让每个版本
    都不可变：回滚到更早的版本时，必须恢复支撑*那个*版本的证据，而 `INV-10` 也必须能
    针对真正被发布的那个版本进行核对。
    """

    __tablename__ = "report_sources"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('metric', 'readme', 'prior_report')",
            name="source_type_valid",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_report_sources_report_section", "report_id", "version", "section_key"),
        Index("ix_report_sources_repository", "repository_id"),
    )

    id: Mapped[uint64_pk]
    report_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("reports.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    repository_id: Mapped[int | None] = mapped_column(unsigned_bigint(), nullable=True)
    section_key: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metric_snapshot_id: Mapped[int | None] = mapped_column(unsigned_bigint(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )


class ReportDraft(Base, TimestampMixin):
    """在提升为报告之前，存放 Agent 撰写章节的工作草稿。"""

    __tablename__ = "report_drafts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'superseded', 'promoted')",
            name="status_valid",
        ),
        UniqueConstraint("agent_run_id"),
    )

    id: Mapped[uint64_pk]
    agent_run_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    topic_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("topics.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")


class ReportSection(Base, TimestampMixin):
    """报告草稿内一个带版本的章节。"""

    __tablename__ = "report_sections"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("report_draft_id", "section_key", "version"),
        Index(
            "ix_report_sections_draft_key_digest",
            "report_draft_id",
            "section_key",
            "content_digest",
        ),
    )

    id: Mapped[uint64_pk]
    report_draft_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("report_drafts.id", ondelete="CASCADE"),
        nullable=False,
    )
    section_key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_markdown: Mapped[str] = mapped_column(medium_text(), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    citations_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)


class ReportVerification(Base):
    """针对某个报告版本的一条 Verifier 结论，记录在任何发布决策之前。

    这一行是 `INV-5` 可查询的证据：只有当报告的当前版本存在校验结论之后，报告才可以
    发布。四个布尔列与 `A-004` 的各项检查一一对应，因此运维不必解析 JSON 细节，就能
    看出是哪一项检查挡住了发布。
    """

    __tablename__ = "report_verifications"
    __table_args__ = (
        CheckConstraint("verdict IN ('passed', 'failed')", name="verdict_valid"),
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("report_id", "version"),
        Index("ix_report_verifications_report_created", "report_id", "created_at"),
    )

    id: Mapped[uint64_pk]
    report_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("reports.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    structure_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    citations_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    numbers_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sensitivity_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason_codes_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )


class ReportReview(Base):
    """对某个报告版本的一次人工审核决定。"""

    __tablename__ = "report_reviews"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('approve', 'reject', 'request_changes')",
            name="decision_valid",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_report_reviews_report_created", "report_id", "created_at"),
    )

    id: Mapped[uint64_pk]
    report_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("reports.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewer_id: Mapped[int | None] = mapped_column(
        unsigned_bigint(),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_version_id: Mapped[int | None] = mapped_column(
        unsigned_bigint(),
        ForeignKey("report_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )


class ReportShareLink(Base, TimestampMixin):
    """一种 bearer 凭据：让一份已发布的 `PUBLIC` 报告无需登录即可阅读。

    这里只存令牌的摘要：`token_hash`。这也是把它放进数据库的全部安全理由 —— 如果
    这张表泄露，泄露出去的是「有人曾分享过这份报告」，而不是「这里有一个能读到它
    的 URL」。因此明文只存在于创建该行的那次响应里，`list_share_links` 永远无法再
    把它交出来。

    `expires_at` 为 `NULL` 表示「不过期」。`revoked_at` 为 `NULL` 表示「未被吊销」。
    其余情况 —— 令牌错误、已吊销、已过期、报告不再是 `PUBLIC` 或不再处于已发布状态
    —— 全部归结为同一个 `None`，因为区分它们会把这条路由变成一个预言机，去回答
    「这份报告到底存不存在」。
    """

    __tablename__ = "report_share_links"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_report_share_links_token_hash"),
        Index("ix_report_share_links_report_revoked", "report_id", "revoked_at"),
    )

    id: Mapped[uint64_pk]
    report_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("reports.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        unsigned_bigint(),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_accessed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    access_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
