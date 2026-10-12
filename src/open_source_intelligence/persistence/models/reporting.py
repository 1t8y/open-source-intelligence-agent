"""Report, draft, section, and citation persistence models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
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


class Report(Base):
    """A published or in-review weekly report generated from an Agent run."""

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


class ReportSource(Base):
    """One traceable evidence reference backing a report section."""

    __tablename__ = "report_sources"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('metric', 'readme', 'prior_report')",
            name="source_type_valid",
        ),
        Index("ix_report_sources_report_section", "report_id", "section_key"),
        Index("ix_report_sources_repository", "repository_id"),
    )

    id: Mapped[uint64_pk]
    report_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("reports.id", ondelete="CASCADE"),
        nullable=False,
    )
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
    """Working draft that holds Agent-written sections before promotion to a report."""

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
    """One versioned section inside a report draft."""

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
