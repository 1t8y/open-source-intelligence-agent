"""`save_report_section` 的实现：幂等的草稿章节持久化。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.reporting import ReportDraft, ReportSection

from .context import ToolContext
from .contracts import SaveReportSectionInput, SectionRef
from .outcome import ToolOutcome


def _load_or_create_draft(session: Session, ctx: ToolContext) -> ReportDraft:
    """返回本次运行唯一的草稿，首次写入时创建它。"""
    draft = session.scalar(select(ReportDraft).where(ReportDraft.agent_run_id == ctx.agent_run_id))
    if draft is None:
        draft = ReportDraft(
            agent_run_id=ctx.agent_run_id,
            topic_id=ctx.topic_id,
            status="draft",
        )
        session.add(draft)
        session.flush()
    return draft


def save_report_section(
    session: Session,
    ctx: ToolContext,
    payload: SaveReportSectionInput,
) -> ToolOutcome:
    """保存一个章节版本；内容摘要相同时直接返回已有版本。"""
    draft = _load_or_create_draft(session, ctx)
    existing = session.scalar(
        select(ReportSection).where(
            ReportSection.report_draft_id == draft.id,
            ReportSection.section_key == payload.section_key,
            ReportSection.content_digest == payload.content_digest,
        )
    )
    if existing is not None:
        section = existing
    else:
        latest_version = session.scalar(
            select(func.max(ReportSection.version)).where(
                ReportSection.report_draft_id == draft.id,
                ReportSection.section_key == payload.section_key,
            )
        )
        section = ReportSection(
            report_draft_id=draft.id,
            section_key=payload.section_key,
            version=(latest_version or 0) + 1,
            content_markdown=payload.content_markdown,
            content_digest=payload.content_digest,
            citations_json=[citation.model_dump() for citation in payload.citations],
        )
        session.add(section)
        session.flush()
    saved_at = section.created_at or utc_now()
    ref = SectionRef(
        report_draft_id=draft.id,
        section_key=section.section_key,
        version=section.version,
        saved_at=saved_at,
    )
    return ToolOutcome(data=ref, result_count=1, truncated=False)
