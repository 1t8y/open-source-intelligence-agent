"""Tests for draft promotion, report versioning, and citation validation."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from agent_support import seed_topic
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from support import NOW, Workspace, build_settings

from open_source_intelligence.agent.runtime import run_research_agent
from open_source_intelligence.agent.tools.contracts import REPORT_SECTION_KEYS, CitationRef
from open_source_intelligence.agent.tools.truncation import content_digest
from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportDraft,
    ReportSection,
    ReportSource,
    ReportVersion,
)
from open_source_intelligence.reports.citations import resolve_citation
from open_source_intelligence.reports.composition import generate_report
from open_source_intelligence.reports.errors import ReportGenerationError


@dataclass(slots=True)
class DraftedRun:
    """One completed Agent run with its persisted draft."""

    agent_run_id: int
    draft_id: int


@pytest.fixture
def drafted_run(session: Session, workspace: Workspace) -> Iterator[DraftedRun]:
    """Generate one deterministic draft through the real Agent runtime."""
    seeded = seed_topic(workspace, repo_count=1)
    result = run_research_agent(
        session,
        topic_id=seeded.topic.id,
        settings=build_settings(),
        now=NOW,
    )
    draft = session.scalar(
        select(ReportDraft).where(ReportDraft.agent_run_id == result.agent_run_id)
    )
    assert draft is not None
    yield DraftedRun(agent_run_id=result.agent_run_id, draft_id=draft.id)


def _latest_section(session: Session, draft_id: int, section_key: str) -> ReportSection:
    section = session.scalar(
        select(ReportSection)
        .where(
            ReportSection.report_draft_id == draft_id,
            ReportSection.section_key == section_key,
        )
        .order_by(ReportSection.version.desc())
        .limit(1)
    )
    assert section is not None
    return section


def test_generate_report_composes_sections_and_traceable_sources(
    session: Session,
    drafted_run: DraftedRun,
) -> None:
    """A valid draft becomes one report with the full template and source rows."""
    result = generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)

    report = session.get(Report, result.report_id)
    assert report is not None
    assert result.version == 1
    assert result.source_count > 0
    assert report.status == "draft"
    assert report.current_version == 1
    assert report.content_markdown.startswith("# AI Agent 开源技术情报周报")
    for title in ("本周摘要", "趋势观察", "重点项目", "风险与不确定性", "行动建议", "附录"):
        assert f"## {title}" in report.content_markdown

    versions = session.scalars(
        select(ReportVersion).where(ReportVersion.report_id == report.id)
    ).all()
    assert len(versions) == 1
    assert versions[0].version == 1
    assert versions[0].content_markdown == report.content_markdown

    sources = session.scalars(select(ReportSource).where(ReportSource.report_id == report.id)).all()
    assert sources
    assert {source.section_key for source in sources} <= set(REPORT_SECTION_KEYS)
    assert all(source.source_ref for source in sources)
    assert all(source.source_url and source.source_url.startswith("https://") for source in sources)
    assert any(source.metric_snapshot_id is not None for source in sources)

    draft = session.get(ReportDraft, drafted_run.draft_id)
    assert draft is not None
    assert draft.status == "promoted"


def test_generate_report_is_idempotent_for_unchanged_draft(
    session: Session,
    drafted_run: DraftedRun,
) -> None:
    """Re-running the same draft does not create a duplicate version or report."""
    first = generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)
    second = generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)

    assert second.created is False
    assert second.version_incremented is False
    assert second.version == 1
    assert int(session.scalar(select(func.count()).select_from(Report)) or 0) == 1
    assert int(session.scalar(select(func.count()).select_from(ReportVersion)) or 0) == 1
    assert (
        int(session.scalar(select(func.count()).select_from(ReportSource)) or 0)
        == first.source_count
    )


def test_changed_section_increments_report_version(
    session: Session,
    drafted_run: DraftedRun,
) -> None:
    """A changed draft creates a new immutable report version."""
    generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)
    section = _latest_section(session, drafted_run.draft_id, "summary")
    section.content_markdown = section.content_markdown + "\n补充观察。"
    section.content_digest = content_digest(section.content_markdown)
    session.commit()

    result = generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)

    assert result.version == 2
    assert result.version_incremented is True
    report = session.get(Report, result.report_id)
    assert report is not None and report.current_version == 2
    versions = session.scalars(
        select(ReportVersion)
        .where(ReportVersion.report_id == report.id)
        .order_by(ReportVersion.version)
    ).all()
    assert [version.version for version in versions] == [1, 2]


def test_missing_citations_are_rejected(
    session: Session,
    drafted_run: DraftedRun,
) -> None:
    """A generated report section may not be promoted without evidence."""
    section = _latest_section(session, drafted_run.draft_id, "summary")
    section.citations_json = []
    session.commit()

    with pytest.raises(ReportGenerationError, match="has no citations"):
        generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)


def test_invalid_metric_reference_is_rejected_before_write(
    session: Session,
    drafted_run: DraftedRun,
) -> None:
    """Fabricated metric references fail without creating a partial report."""
    section = _latest_section(session, drafted_run.draft_id, "summary")
    section.citations_json = [
        {
            "source_type": "metric",
            "source_ref": "repo_snapshots:999999:2099-01-01",
            "source_url": None,
        }
    ]
    session.commit()

    with pytest.raises(ReportGenerationError, match="metric source not found"):
        generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)
    assert int(session.scalar(select(func.count()).select_from(Report)) or 0) == 0


def test_published_report_cannot_be_overwritten(
    session: Session,
    drafted_run: DraftedRun,
) -> None:
    """Published content is immutable until an explicit review workflow changes it."""
    generated = generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)
    report = session.get(Report, generated.report_id)
    assert report is not None
    report.status = "published"
    section = _latest_section(session, drafted_run.draft_id, "summary")
    section.content_markdown = section.content_markdown + "\n新内容。"
    section.content_digest = content_digest(section.content_markdown)
    session.commit()

    with pytest.raises(ReportGenerationError, match="not editable"):
        generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)


def test_prior_report_citation_requires_published_status(
    session: Session,
    workspace: Workspace,
) -> None:
    """Published historical reports resolve; drafts cannot become citations."""
    topic = workspace.add_topic()
    source = workspace.add_source()
    workspace.add_repository(source_id=source.id)
    run = workspace.add_agent_run(topic.id)
    prior = workspace.add_report(
        topic_id=topic.id,
        agent_run_id=run.id,
        status="published",
    )
    citation = CitationRef(
        source_type="prior_report",
        source_ref=f"reports:{prior.id}",
    )

    resolved = resolve_citation(session, citation)
    assert resolved.source_type == "prior_report"
    assert resolved.repository_id is None

    prior.status = "draft"
    session.commit()
    with pytest.raises(ReportGenerationError, match="published prior report not found"):
        resolve_citation(session, citation)
