"""`search_prior_reports` 的实现：按相关性排序的往期报告摘要。

默认排序是时间新近（最新的在前），这也是一个受 `limit` 约束的列表本来该有的
含义。当调用方给了 `query`，报告改为按与该查询的词法相关性排序（见
`agent/retrieval/vector.py`），这样「最贴近本周主题的往期结论」就不会只是
碰巧最后写下的那一份。查询排序全程离线，并在查询为空或匹配不到任何内容时
退化为按时间新近排序。
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

#: 在做相关性排序之前先取回多少候选，好让排序有挑选的余地，而不是只给一个
#: 已被截断的头部重新排序。这里取用户上限的一个小倍数：摘要本身很短，多读
#: 几条的代价很低。
CANDIDATE_MULTIPLE = 4


def search_prior_reports(
    session: Session,
    ctx: ToolContext,
    payload: SearchPriorReportsInput,
) -> ToolOutcome:
    """返回报告级摘要并按相关性排序，绝不泄露未授权的报告。"""
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

    # 先构造摘要（`source_refs` 需要对每份报告单独查询），然后再排序。
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
        # 按位置而不是按文本映射回去：两份报告可能标题相同、开头段落也相同，
        # 此时用文本做键的查找会把第一份返回两次却丢掉另一份 —— 一个消失得
        # 悄无声息、也没人报告缺失的基线。
        ordered = [summaries[item.index] for item in ranked]
    else:
        ordered = summaries[: payload.limit]

    return ToolOutcome(data=ordered, result_count=len(ordered), truncated=truncated)
