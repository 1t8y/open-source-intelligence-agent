"""Compose a versioned, traceable report from Agent draft sections."""

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
    """Summary of one draft-to-report promotion."""

    report_id: int
    agent_run_id: int
    version: int
    source_count: int
    created: bool
    version_incremented: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe response payload."""
        return {
            "report_id": self.report_id,
            "agent_run_id": self.agent_run_id,
            "version": self.version,
            "source_count": self.source_count,
            "created": self.created,
            "version_incremented": self.version_incremented,
        }


def _latest_sections(session: Session, report_draft_id: int) -> dict[str, ReportSection]:
    """Return the latest version of every section in one draft."""
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
    """Validate the JSON citations attached to one draft section."""
    try:
        return [CitationRef.model_validate(item) for item in (section.citations_json or [])]
    except ValidationError as exc:
        raise ReportGenerationError(
            f"section {section.section_key} contains invalid citations: {exc}"
        ) from exc


def _candidate_full_names(session: Session, topic_id: int) -> list[str]:
    """Return the repositories the run was allowed to research, in rank order.

    This is the candidate set, not the cited set. A repository that was studied and then not
    quoted is still part of the input the conclusions were drawn from, and leaving it out of the
    provenance line would understate what the report could see.
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


def _compose_from_resolved(
    title: str,
    sections: dict[str, ReportSection],
    resolved_sources: dict[tuple[str, str], ResolvedCitation],
    *,
    window_start: datetime,
    window_end: datetime,
    candidate_full_names: Sequence[str] = (),
) -> str:
    """Render the fixed report template in deterministic section order.

    A short provenance block follows the title so a reader can see, without digging into the
    database, exactly which candidate set and snapshot window produced this report. That is
    what makes two consecutive reports with different conclusions explainable: the input
    (candidates + window) is printed next to the output (conclusions).
    """
    lines = [f"# {title}", ""]

    # Provenance: the candidate set actually selected for the run, the snapshot window, and how
    # many distinct snapshots back the numbers. The observation count is a count of evidence, so
    # it is de-duplicated: one snapshot cited by three sections is one observation.
    candidates = list(candidate_full_names)
    observation_points = len(
        {
            citation.source_ref
            for (_, _), citation in resolved_sources.items()
            if citation.source_type == "metric"
        }
    )
    lines.append("> 本轮候选仓库：" + ("、".join(candidates) if candidates else "无"))
    lines.append(
        f"> 快照窗口：{window_start.date().isoformat()} 至 {window_end.date().isoformat()}；"
        f"指标观测点 {observation_points} 个"
    )
    lines.append("")

    for key in REPORT_SECTION_KEYS:
        section = sections[key]
        lines.extend([f"## {SECTION_TITLES[key]}", "", section.content_markdown.strip(), ""])

    if resolved_sources:
        lines.extend(["## 引用与证据", ""])
        # De-duplicate by source_ref across sections: one snapshot cited by three sections is
        # one piece of evidence, not three. Group by type so a reader sees the metric snapshots
        # first, then README and prior-report references.
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
    """Promote the latest Agent draft into a versioned report and citation rows."""
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
            # A reviewer asked for changes; regenerating is the response, so the report has
            # to re-enter the draft queue even when the Agent lands on identical content.
            # Without this the documented `changes_requested -> draft` edge is unreachable
            # and the report is stuck in a state nothing may publish from.
            transition_report(report, "draft")
            report.publish_at = None
        if report.content_markdown != markdown:
            report.current_version += 1
            report.content_markdown = markdown
            report.generated_at = generated_at
            # Go through the state machine rather than assigning `status` directly: it is the
            # only writer of `reports.status`, and a direct write would silently keep working
            # even if this branch later became reachable from a state it must not be.
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

    # Only this version's citation rows are replaced. Earlier versions keep theirs so a
    # rollback can restore the evidence that backed that version, and `INV-10` can always be
    # checked against the version actually being published.
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
