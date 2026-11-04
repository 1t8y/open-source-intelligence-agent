"""把 Agent 的草稿章节编排成一份带版本、可追溯的报告。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.tools.contracts import REPORT_SECTION_KEYS, CitationRef
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.agent import AgentRun
from open_source_intelligence.persistence.models.intelligence import (
    Repository,
    RepoSnapshot,
    Topic,
    TopicRepository,
)
from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportDraft,
    ReportSection,
    ReportSource,
    ReportVersion,
)
from open_source_intelligence.security.audit import record_audit_event

from .citations import ResolvedCitation, resolve_citation
from .errors import ReportGenerationError
from .state import transition_report

SECTION_TITLES = {
    "summary": "本周摘要",
    "trend": "趋势观察",
    "highlights": "重点项目",
    "risks": "风险与不确定性",
    "recommendation": "行动建议",
    "appendix": "附录",
}
_NUMBER_PATTERN = re.compile(r"\d")


@dataclass(frozen=True, slots=True)
class ReportGenerationResult:
    """一次「草稿提升为报告」的摘要。"""

    report_id: int
    agent_run_id: int
    version: int
    source_count: int
    created: bool
    version_incremented: bool

    def to_dict(self) -> dict[str, Any]:
        """返回可安全转 JSON 的响应载荷。"""
        return {
            "report_id": self.report_id,
            "agent_run_id": self.agent_run_id,
            "version": self.version,
            "source_count": self.source_count,
            "created": self.created,
            "version_incremented": self.version_incremented,
        }


def _latest_sections(session: Session, report_draft_id: int) -> dict[str, ReportSection]:
    """返回一份草稿中每个章节的最新版本。"""
    rows = session.scalars(
        select(ReportSection)
        .where(ReportSection.report_draft_id == report_draft_id)
        .order_by(ReportSection.section_key, ReportSection.version.desc())
    ).all()
    latest: dict[str, ReportSection] = {}
    for row in rows:
        latest.setdefault(row.section_key, row)
    return latest


def _parse_citations(section: ReportSection) -> list[CitationRef]:
    """校验挂在某个草稿章节上的 JSON 引用。"""
    try:
        return [CitationRef.model_validate(item) for item in (section.citations_json or [])]
    except ValidationError as exc:
        raise ReportGenerationError(
            f"section {section.section_key} contains invalid citations: {exc}"
        ) from exc


def _candidate_full_names(session: Session, topic_id: int) -> list[str]:
    """按排名顺序返回本次运行被允许研究的仓库。

    这是候选集合，不是被引用的集合。一个研究了却没被引用的仓库，依然是结论所依据的
    输入的一部分；把它从来源说明行里去掉，就等于低报了这份报告实际能看到什么。
    """
    rows = session.execute(
        select(Repository.full_name)
        .join(TopicRepository, TopicRepository.repository_id == Repository.id)
        .where(
            TopicRepository.topic_id == topic_id,
            TopicRepository.selected_for_agent.is_(True),
        )
        .order_by(TopicRepository.rank.asc())
    ).all()
    return [str(row[0]) for row in rows]


def _movement_rows(
    session: Session,
    *,
    topic_id: int,
    window_start: datetime,
    window_end: datetime,
) -> list[tuple[str, int]]:
    """返回每个研究候选在报告窗口内的 stars 净增。

    在这里算，而不是去问模型。让语言模型去除两个隔了一段话才读到的数字，它错得足够
    频繁，足以影响结论 —— 有一次真实运行把 92,667 中的 84,713 报成了「64.8%」。百分比
    是算术，本来就能从这份报告所依据的行里推出来；而读者若不自己动手做除法，就分辨不出
    一个百分比是对是错。
    """
    rows = session.execute(
        select(Repository.id, Repository.full_name, TopicRepository.rank)
        .join(TopicRepository, TopicRepository.repository_id == Repository.id)
        .where(
            TopicRepository.topic_id == topic_id,
            TopicRepository.selected_for_agent.is_(True),
        )
        .order_by(TopicRepository.rank.asc())
    ).all()
    movements: list[tuple[str, int]] = []
    for repository_id, full_name, _rank in rows:
        series = (
            session.execute(
                select(RepoSnapshot.stars)
                .where(
                    RepoSnapshot.repository_id == repository_id,
                    RepoSnapshot.snapshot_date >= window_start.date(),
                    RepoSnapshot.snapshot_date <= window_end.date(),
                )
                .order_by(RepoSnapshot.snapshot_date.asc())
            )
            .scalars()
            .all()
        )
        if len(series) < 2:
            continue
        movements.append((str(full_name), int(series[-1]) - int(series[0])))
    return movements


def _compose_from_resolved(
    title: str,
    sections: dict[str, ReportSection],
    resolved_sources: dict[tuple[str, str], ResolvedCitation],
    *,
    window_start: datetime,
    window_end: datetime,
    candidate_full_names: Sequence[str] = (),
    movements: Sequence[tuple[str, int]] = (),
) -> str:
    """按确定的章节顺序渲染固定的报告模板。

    标题之后跟一段简短的来源说明，读者不必翻数据库就能看清这份报告究竟由哪个候选集合、
    哪个快照窗口产生。这正是「两期结论不同的报告可以被解释」的原因：输入（候选 + 窗口）
    就印在输出（结论）旁边。
    """
    lines = [f"# {title}", ""]

    # 来源说明：本次运行实际选中的候选集合、快照窗口，以及有多少个不同的快照支撑了这些
    # 数字。计数已去重 —— 被三个章节引用的同一个快照只算一个 —— 而且标签用「引用快照」
    # 而不是「观测点」：正文里还会报每条序列*覆盖*了多少个快照，两者都叫观测点，会让
    # 同一份报告用同一个词给出两个不同的总数。
    candidates = list(candidate_full_names)
    cited_snapshots = len(
        {
            citation.source_ref
            for (_, _), citation in resolved_sources.items()
            if citation.source_type == "metric"
        }
    )
    lines.append("> 本轮候选仓库：" + ("、".join(candidates) if candidates else "无"))
    lines.append(
        f"> 快照窗口：{window_start.date().isoformat()} 至 {window_end.date().isoformat()}；"
        f"引用快照 {cited_snapshots} 个"
    )
    lines.append("")

    # 读者最先核对的数字，由系统算出而非由模型撰写。把它们放在正文之上，某个章节里的
    # 错误数字就会与表格对不上，而不是孤零零地成为页面上唯一的数字。
    if movements:
        total = sum(delta for _, delta in movements)
        lines.extend(
            ["**关键数字（系统统计）**", "", "| 仓库 | 净增 stars | 占合计 |", "|---|---:|---:|"]
        )
        for full_name, delta in movements:
            share = f"{delta * 100.0 / total:.1f}%" if total > 0 else "—"
            lines.append(f"| {full_name} | {delta:+,} | {share} |")
        total_share = "100%" if total > 0 else "—"
        lines.extend([f"| **合计** | **{total:+,}** | {total_share} |", ""])

    for key in REPORT_SECTION_KEYS:
        section = sections[key]
        lines.extend([f"## {SECTION_TITLES[key]}", "", section.content_markdown.strip(), ""])

    if resolved_sources:
        lines.extend(["## 引用与证据", ""])
        # 跨章节按 source_ref 去重：被三个章节引用的同一个快照是一份证据，不是三份。
        # 按类型分组，读者先看到指标快照，再看 README 与历史报告引用。
        unique: dict[str, ResolvedCitation] = {}
        for (_, _), citation in resolved_sources.items():
            unique.setdefault(citation.source_ref, citation)
        for type_key, label in (
            ("metric", "指标快照"),
            ("readme", "README"),
            ("prior_report", "历史报告"),
        ):
            typed = [c for c in unique.values() if c.source_type == type_key]
            if not typed:
                continue
            lines.extend([f"### {label}", ""])
            for citation in typed:
                if citation.source_url:
                    lines.append(f"- [{citation.source_ref}]({citation.source_url})")
                else:
                    lines.append(f"- `{citation.source_ref}`")
            lines.append("")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_report(
    session: Session,
    *,
    agent_run_id: int,
    title: str | None = None,
    now: datetime | None = None,
) -> ReportGenerationResult:
    """把最新的 Agent 草稿提升为带版本的报告与引用行。"""
    agent_run = session.get(AgentRun, agent_run_id)
    if agent_run is None:
        raise ReportGenerationError(f"agent run {agent_run_id} does not exist")
    draft = session.scalar(select(ReportDraft).where(ReportDraft.agent_run_id == agent_run_id))
    if draft is None:
        raise ReportGenerationError(f"agent run {agent_run_id} has no report draft")
    topic = session.get(Topic, draft.topic_id)
    if topic is None:
        raise ReportGenerationError(f"topic {draft.topic_id} does not exist")

    sections = _latest_sections(session, draft.id)
    missing = [key for key in REPORT_SECTION_KEYS if key not in sections]
    if missing:
        raise ReportGenerationError(f"draft is missing sections: {', '.join(missing)}")

    resolved_sources: dict[tuple[str, str], ResolvedCitation] = {}
    for key in REPORT_SECTION_KEYS:
        section = sections[key]
        citations = _parse_citations(section)
        if not citations:
            raise ReportGenerationError(f"section {key} has no citations")
        resolved_for_section = [resolve_citation(session, citation) for citation in citations]
        if _NUMBER_PATTERN.search(section.content_markdown) and not any(
            item.source_type == "metric" for item in resolved_for_section
        ):
            raise ReportGenerationError(f"section {key} contains numbers without metric citations")
        for citation in resolved_for_section:
            resolved_sources[(key, citation.source_ref)] = citation

    generated_at = now or utc_now()
    resolved_title = title or (
        f"{topic.name} 开源技术情报周报（{agent_run.window_end.date().isoformat()}）"
    )
    markdown = _compose_from_resolved(
        resolved_title,
        sections,
        resolved_sources,
        window_start=agent_run.window_start,
        window_end=agent_run.window_end,
        candidate_full_names=_candidate_full_names(session, draft.topic_id),
        movements=_movement_rows(
            session,
            topic_id=draft.topic_id,
            window_start=agent_run.window_start,
            window_end=agent_run.window_end,
        ),
    )

    report = session.scalar(
        select(Report)
        .where(Report.agent_run_id == agent_run_id)
        .order_by(Report.id.desc())
        .limit(1)
    )
    created = report is None
    version_incremented = False
    if report is None:
        report = Report(
            agent_run_id=agent_run_id,
            topic_id=draft.topic_id,
            owner_id=topic.created_by,
            title=resolved_title,
            current_version=1,
            content_markdown=markdown,
            status="draft",
            visibility="TEAM",
            sensitivity="INTERNAL",
            publish_mode=topic.publish_mode,
            generated_at=generated_at,
        )
        session.add(report)
        session.flush()
        version = 1
    else:
        if report.status not in {"draft", "changes_requested"}:
            raise ReportGenerationError(
                f"report {report.id} is not editable in status {report.status}"
            )
        if report.owner_id is None:
            report.owner_id = topic.created_by
        report.title = resolved_title
        report.topic_id = draft.topic_id
        report.publish_mode = topic.publish_mode
        if report.status == "changes_requested":
            # 评审要求修改，重新生成就是回应，所以即使 Agent 产出的内容完全一样，报告
            # 也必须重新进入草稿队列。缺了这一步，文档里那条
            # `changes_requested -> draft` 边就永远走不到，报告会卡在一个任何发布路径
            # 都进不去的状态里。
            transition_report(report, "draft")
            report.publish_at = None
        if report.content_markdown != markdown:
            report.current_version += 1
            report.content_markdown = markdown
            report.generated_at = generated_at
            # 走状态机而不是直接给 `status` 赋值：它是 `reports.status` 唯一的写入方，
            # 直接写会悄悄地继续生效，哪怕这个分支日后真从一个不该迁出的状态被走到。
            transition_report(report, "draft")
            version_incremented = True
        version = report.current_version

    existing_version = session.scalar(
        select(ReportVersion).where(
            ReportVersion.report_id == report.id,
            ReportVersion.version == version,
        )
    )
    if existing_version is None:
        session.add(
            ReportVersion(
                report_id=report.id,
                version=version,
                content_markdown=markdown,
                change_reason="generated_from_agent_draft",
                created_at=generated_at,
            )
        )

    # 只替换这一版的引用行。更早的版本保留各自的引用，回滚才能还原支撑那一版的证据，
    # `INV-10` 也才能始终针对真正在发布的那个版本做检查。
    session.execute(
        delete(ReportSource).where(
            ReportSource.report_id == report.id,
            ReportSource.version == version,
        )
    )
    for (section_key, _), citation in resolved_sources.items():
        session.add(
            ReportSource(
                report_id=report.id,
                version=version,
                repository_id=citation.repository_id,
                section_key=section_key,
                source_type=citation.source_type,
                source_url=citation.source_url,
                source_ref=citation.source_ref,
                metric_snapshot_id=citation.metric_snapshot_id,
            )
        )
    draft.status = "promoted"
    record_audit_event(
        session,
        action="report.generate",
        resource_type="report",
        resource_id=str(report.id),
        result="success",
        metadata_json={
            "agent_run_id": agent_run_id,
            "version": version,
            "source_count": len(resolved_sources),
        },
    )
    session.commit()
    return ReportGenerationResult(
        report_id=report.id,
        agent_run_id=agent_run_id,
        version=version,
        source_count=len(resolved_sources),
        created=created,
        version_incremented=version_incremented,
    )
