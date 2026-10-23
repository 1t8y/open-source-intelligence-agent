"""Tests for draft promotion, report versioning, and citation validation."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

import pytest
from agent_support import seed_topic
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from support import NOW, Workspace, build_settings

from open_source_intelligence.agent.runtime import run_research_agent
from open_source_intelligence.agent.tools.contracts import REPORT_SECTION_KEYS, CitationRef
from open_source_intelligence.core.text import content_digest
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
    topic = session.get(Topic, report.topic_id)
    assert topic is not None
    assert report.owner_id == topic.created_by
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


def test_generated_sources_anchor_on_full_name_not_mutable_id(
    session: Session,
    drafted_run: DraftedRun,
) -> None:
    """Generated reports cite repositories by stable `owner/name`, not the auto-increment id."""
    result = generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)
    report = session.get(Report, result.report_id)
    assert report is not None

    sources = session.scalars(select(ReportSource).where(ReportSource.report_id == report.id)).all()
    metric_refs = [s.source_ref for s in sources if s.source_type == "metric"]
    assert metric_refs
    # Every metric citation now anchors on a full name (contains a `/`) rather than a bare id.
    assert all("/" in ref.split(":")[1] for ref in metric_refs)
    # And the rendered report body carries the same stable form.
    for ref in metric_refs:
        assert ref in report.content_markdown


def test_full_name_citation_resolves_after_the_id_changes(
    session: Session,
    drafted_run: DraftedRun,
) -> None:
    """A citation by full name resolves even when the repository id is not the drafting key."""
    generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)
    report = session.scalar(select(Report).order_by(Report.id.desc()))
    assert report is not None
    source = session.scalar(
        select(ReportSource).where(
            ReportSource.report_id == report.id,
            ReportSource.source_type == "metric",
        )
    )
    assert source is not None
    citation = CitationRef(source_type="metric", source_ref=source.source_ref)
    resolved = resolve_citation(session, citation)
    assert resolved.source_ref == source.source_ref
    assert resolved.repository_id is not None


def test_report_body_records_candidates_and_window(
    session: Session,
    drafted_run: DraftedRun,
) -> None:
    """The report body carries a provenance block: candidates, window, observation count."""
    result = generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)
    report = session.get(Report, result.report_id)
    assert report is not None
    assert "本轮候选仓库：" in report.content_markdown
    assert "快照窗口：" in report.content_markdown
    assert "指标观测点" in report.content_markdown


def test_evidence_section_deduplicates_and_groups_sources(
    session: Session,
    drafted_run: DraftedRun,
) -> None:
    """One snapshot cited by several sections appears once in the evidence section."""
    result = generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)
    report = session.get(Report, result.report_id)
    assert report is not None

    evidence = report.content_markdown.split("## 引用与证据", 1)[1]
    # Grouped under the metric heading.
    assert "### 指标快照" in evidence
    # No single source_ref should appear twice in the evidence block.
    metric_refs = [
        line
        for line in evidence.splitlines()
        if line.strip().startswith("-") and "repo_snapshots:" in line
    ]
    assert len(metric_refs) == len(set(metric_refs))


def test_provenance_counts_evidence_not_citations(
    session: Session,
    drafted_run: DraftedRun,
) -> None:
    """The provenance line describes the run's input, not the citation list.

    Two numbers were wrong in the same direction: "指标观测点" counted `(section, ref)` pairs, so
    one snapshot quoted by three sections read as three observations, and "本轮候选仓库" was derived
    from the cited repositories, so a candidate that was researched but not quoted disappeared
    from the list of what the run could see.
    """
    result = generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)
    report = session.get(Report, result.report_id)
    assert report is not None

    metric_refs = {
        row.source_ref
        for row in session.scalars(
            select(ReportSource).where(
                ReportSource.report_id == report.id,
                ReportSource.source_type == "metric",
            )
        ).all()
    }
    body = report.content_markdown
    assert f"指标观测点 {len(metric_refs)} 个" in body

    # Candidate links are the run's input; the block must name them all, cited or not.
    candidate_names = {
        row[0]
        for row in session.execute(
            select(Repository.full_name)
            .join(TopicRepository, TopicRepository.repository_id == Repository.id)
            .where(TopicRepository.selected_for_agent.is_(True))
        ).all()
    }
    provenance_line = next(
        line for line in body.splitlines() if line.startswith("> 本轮候选仓库：")
    )
    for full_name in candidate_names:
        assert full_name in provenance_line


def test_a_duplicate_full_name_resolves_through_the_snapshot(
    session: Session,
    workspace: Workspace,
) -> None:
    """A full name is not unique on its own, so the date decides which source the citation means.

    `repositories` is unique on `(source_id, provider_repo_id)`, so two data sources can hold the
    same `owner/name`. Resolving the name first and then hoping that row owns the snapshot made the
    answer depend on insertion order: a citation that clearly named the second source's snapshot
    failed as "not found".
    """
    first_source = workspace.add_source()
    second_source = workspace.add_source(source_type="huggingface")
    earlier = workspace.add_repository(
        source_id=first_source.id,
        provider_repo_id="dup-a",
        full_name="acme/duplicated",
    )
    later = workspace.add_repository(
        source_id=second_source.id,
        provider_repo_id="dup-b",
        full_name="acme/duplicated",
    )
    workspace.add_snapshot(earlier.id, date(2026, 10, 5), stars=100, forks=10)
    workspace.add_snapshot(later.id, date(2026, 10, 11), stars=200, forks=20)

    resolved = resolve_citation(
        session,
        CitationRef(
            source_type="metric",
            source_ref="repo_snapshots:acme/duplicated:2026-10-11",
        ),
    )

    assert resolved.repository_id == later.id
