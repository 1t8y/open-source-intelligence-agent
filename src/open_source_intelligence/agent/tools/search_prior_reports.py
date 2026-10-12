"""`search_prior_reports` implementation: summaries of earlier published reports."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.reporting import Report, ReportSource

from .context import ToolContext
from .contracts import (
    TOOL_ACCESS_DENIED,
    ReportSummary,
    SearchPriorReportsInput,
)
from .errors import ToolExecutionError
from .outcome import ToolOutcome
from .truncation import clean_untrusted_text, truncate_text

SUMMARY_CHARS = 500
SUMMARY_SECTION_KEY = "summary"


def search_prior_reports(
    session: Session,
    ctx: ToolContext,
    payload: SearchPriorReportsInput,
) -> ToolOutcome:
    """Return report-level summaries, never leaking unauthorized reports."""
    if not ctx.can_access_topic(payload.topic_id):
        raise ToolExecutionError(
            TOOL_ACCESS_DENIED,
            "topic is outside the authorized scope",
        )
    stmt = (
        select(Report)
        .where(
            Report.topic_id == payload.topic_id,
            Report.generated_at < payload.before,
            Report.status == "published",
        )
        .order_by(Report.generated_at.desc())
        .limit(payload.limit + 1)
    )
    if payload.repo_id is not None:
        stmt = stmt.where(
            Report.id.in_(
                select(ReportSource.report_id).where(ReportSource.repository_id == payload.repo_id)
            )
        )
    reports = list(session.scalars(stmt).all())
    truncated = len(reports) > payload.limit
    summaries: list[ReportSummary] = []
    for report in reports[: payload.limit]:
        refs = session.scalars(
            select(ReportSource.source_ref)
            .where(
                ReportSource.report_id == report.id,
                ReportSource.source_ref.is_not(None),
            )
            .distinct()
        ).all()
        summary_text, _ = truncate_text(
            clean_untrusted_text(report.content_markdown),
            SUMMARY_CHARS,
        )
        summaries.append(
            ReportSummary(
                report_id=report.id,
                title=report.title,
                generated_at=report.generated_at,
                section_key=SUMMARY_SECTION_KEY,
                summary=summary_text,
                source_refs=[str(ref) for ref in refs if ref],
            )
        )
    return ToolOutcome(data=summaries, result_count=len(summaries), truncated=truncated)
