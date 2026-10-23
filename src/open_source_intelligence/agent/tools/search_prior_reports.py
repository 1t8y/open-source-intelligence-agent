"""`search_prior_reports` implementation: relevance-ranked summaries of earlier reports.

The default ranking is recency (newest first), which is what a `limit`-bounded list means. When
the caller supplies a `query`, the reports are instead ranked by lexical relevance to that query
(see `agent/retrieval/vector.py`), so "the prior conclusion most about this week's topic" is not
whatever happened to be written last. The query ranking stays offline and degrades to recency
when the query is blank or matches nothing.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.retrieval.vector import rank_snippets
from open_source_intelligence.core.text import clean_untrusted_text, truncate_text
from open_source_intelligence.persistence.models.reporting import Report, ReportSource

from .context import ToolContext
from .contracts import (
    TOOL_ACCESS_DENIED,
    ReportSummary,
    SearchPriorReportsInput,
)
from .errors import ToolExecutionError
from .outcome import ToolOutcome

SUMMARY_CHARS = 500
SUMMARY_SECTION_KEY = "summary"

#: How many candidates to fetch before relevance-ranking, so the ranking has room to choose
#: rather than reordering an already-truncated head. Kept a small multiple of the user limit:
#: the summaries are short and the extra reads are cheap.
CANDIDATE_MULTIPLE = 4


def search_prior_reports(
    session: Session,
    ctx: ToolContext,
    payload: SearchPriorReportsInput,
) -> ToolOutcome:
    """Return report-level summaries, relevance-ranked, never leaking unauthorized reports."""
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
    )
    if payload.repo_id is not None:
        stmt = stmt.where(
            Report.id.in_(
                select(ReportSource.report_id).where(
                    ReportSource.repository_id == payload.repo_id,
                    ReportSource.version == Report.current_version,
                )
            )
        )

    reports = list(session.scalars(stmt.limit(payload.limit * CANDIDATE_MULTIPLE)).all())
    truncated = len(reports) > payload.limit

    # Build summaries first (the source_refs need per-report queries), then rank.
    summaries: list[ReportSummary] = []
    for report in reports:
        refs = session.scalars(
            select(ReportSource.source_ref)
            .where(
                ReportSource.report_id == report.id,
                ReportSource.version == report.current_version,
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

    if payload.query and payload.query.strip():
        ranked = rank_snippets(
            payload.query,
            [f"{summary.title}\n{summary.summary}" for summary in summaries],
            top_k=payload.limit,
        )
        # Map back by position, not by text: two reports can carry the same title and the same
        # opening paragraph, and a text-keyed lookup would then return the first one twice while
        # dropping the other — a silently missing baseline that nothing reports as missing.
        ordered = [summaries[item.index] for item in ranked]
    else:
        ordered = summaries[: payload.limit]

    return ToolOutcome(data=ordered, result_count=len(ordered), truncated=truncated)
