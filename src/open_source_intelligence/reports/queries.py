"""支撑报告 API 与管理后台的读侧查询。

Web 层与 API 层不允许自己拼 SQL（`INTERFACES.md` §4），所以列表、筛选和分页都放在
这里。列表查询从授权服务拿到一个访问谓词，并把它下推到数据库执行，这正是
`docs/api-contract.md` §6 的要求：权限过滤不能发生在取数之后。
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
    """一页报告，以及符合筛选条件的总数。"""

    items: tuple[Report, ...]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class ReportDetail:
    """详情视图关于一份报告所需的一切，且不需要额外往返查询。"""

    report: Report
    versions: tuple[ReportVersion, ...]
    sources: tuple[ReportSource, ...]
    verification: ReportVerification | None
    reviews: tuple[ReportReview, ...]


def clamp_page_size(page_size: int) -> int:
    """把请求的分页大小限制在文档约定的 1–100 区间内。"""
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
    """返回调用方有权阅读的一页报告。

    访问谓词是 `WHERE` 子句的一部分，而不是取数之后的过滤，因此调用方永远观察不到
    把自己无权阅读的报告算进去的总数或页位置。
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
    """返回报告及其版本历史、引用、验证结论与评审记录。"""
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
    """把一行报告渲染成 API 列表响应用的字典。"""
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
