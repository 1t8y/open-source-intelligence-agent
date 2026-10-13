"""Read-side queries backing the report API and the admin UI.

The web and API layers are not allowed to build SQL themselves (`INTERFACES.md` §4), so
listing, filtering, and pagination live here. The list query takes an access predicate from
the authorization service and applies it in the database, which is what
`docs/api-contract.md` §6 requires: permission filtering must not happen after fetching.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportReview,
    ReportSource,
    ReportVerification,
    ReportVersion,
)

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class ReportPage:
    """One page of reports plus the total matching the filters."""

    items: tuple[Report, ...]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class ReportDetail:
    """Everything the detail view needs about one report, without extra round trips."""

    report: Report
    versions: tuple[ReportVersion, ...]
    sources: tuple[ReportSource, ...]
    verification: ReportVerification | None
    reviews: tuple[ReportReview, ...]


def clamp_page_size(page_size: int) -> int:
    """Keep a requested page size inside the documented 1-100 range."""
    return max(1, min(page_size, MAX_PAGE_SIZE))


def list_reports(
    session: Session,
    *,
    access_filter: ColumnElement[bool],
    topic_id: int | None = None,
    status: str | None = None,
    visibility: str | None = None,
    sensitivity: str | None = None,
    generated_after: datetime | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> ReportPage:
    """Return one page of reports the caller may read.

    The access predicate is part of the `WHERE` clause rather than a post-filter, so a
    caller can never observe a count or a page position that includes reports they may not
    read.
    """
    resolved_page = max(1, page)
    resolved_size = clamp_page_size(page_size)
    conditions: list[ColumnElement[bool]] = [access_filter]
    if topic_id is not None:
        conditions.append(Report.topic_id == topic_id)
    if status is not None:
        conditions.append(Report.status == status)
    if visibility is not None:
        conditions.append(Report.visibility == visibility)
    if sensitivity is not None:
        conditions.append(Report.sensitivity == sensitivity)
    if generated_after is not None:
        conditions.append(Report.generated_at >= generated_after)

    total = int(session.scalar(select(func.count()).select_from(Report).where(*conditions)) or 0)
    items = session.scalars(
        select(Report)
        .where(*conditions)
        .order_by(Report.generated_at.desc(), Report.id.desc())
        .offset((resolved_page - 1) * resolved_size)
        .limit(resolved_size)
    ).all()
    return ReportPage(
        items=tuple(items),
        page=resolved_page,
        page_size=resolved_size,
        total=total,
    )


def get_report_detail(session: Session, *, report_id: int) -> ReportDetail | None:
    """Return the report with its version history, citations, verdict, and reviews."""
    report = session.get(Report, report_id)
    if report is None:
        return None
    versions = session.scalars(
        select(ReportVersion)
        .where(ReportVersion.report_id == report.id)
        .order_by(ReportVersion.version.desc())
    ).all()
    sources = session.scalars(
        select(ReportSource)
        .where(
            ReportSource.report_id == report.id,
            ReportSource.version == report.current_version,
        )
        .order_by(ReportSource.section_key, ReportSource.id)
    ).all()
    verification = session.scalar(
        select(ReportVerification).where(
            ReportVerification.report_id == report.id,
            ReportVerification.version == report.current_version,
        )
    )
    reviews = session.scalars(
        select(ReportReview)
        .where(ReportReview.report_id == report.id)
        .order_by(ReportReview.created_at.desc(), ReportReview.id.desc())
    ).all()
    return ReportDetail(
        report=report,
        versions=tuple(versions),
        sources=tuple(sources),
        verification=verification,
        reviews=tuple(reviews),
    )


def serialize_report(report: Report, *, topic_name: str | None = None) -> dict[str, Any]:
    """Render one report row for the API list response."""
    payload: dict[str, Any] = {
        "id": report.id,
        "agent_run_id": report.agent_run_id,
        "topic_id": report.topic_id,
        "owner_id": report.owner_id,
        "title": report.title,
        "current_version": report.current_version,
        "status": report.status,
        "visibility": report.visibility,
        "sensitivity": report.sensitivity,
        "publish_mode": report.publish_mode,
        "publish_at": report.publish_at.isoformat() if report.publish_at else None,
        "generated_at": report.generated_at.isoformat(),
        "published_at": report.published_at.isoformat() if report.published_at else None,
    }
    if topic_name is not None:
        payload["topic_name"] = topic_name
    return payload
