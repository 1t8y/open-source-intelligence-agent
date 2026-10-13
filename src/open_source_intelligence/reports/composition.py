"""Compose a versioned, traceable report from Agent draft sections."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.tools.contracts import REPORT_SECTION_KEYS, CitationRef
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.agent import AgentRun
from open_source_intelligence.persistence.models.intelligence import Topic
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


def _compose_from_resolved(
    title: str,
    sections: dict[str, ReportSection],
    resolved_sources: dict[tuple[str, str], ResolvedCitation],
) -> str:
    """Render the fixed report template in deterministic section order."""
    lines = [f"# {title}", ""]
    for key in REPORT_SECTION_KEYS:
        section = sections[key]
        lines.extend([f"## {SECTION_TITLES[key]}", "", section.content_markdown.strip(), ""])

    if resolved_sources:
        lines.extend(["## 引用与证据", ""])
        for index, (_, citation) in enumerate(resolved_sources.items(), start=1):
            label = citation.source_ref
            if citation.source_url:
                lines.append(f"{index}. [{label}]({citation.source_url})")
            else:
                lines.append(f"{index}. `{label}`")
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
    markdown = _compose_from_resolved(resolved_title, sections, resolved_sources)

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
