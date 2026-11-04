"""守护草稿晋升、报告版本与引用校验的用例。"""

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
from open_source_intelligence.reports.composition import _compose_from_resolved, generate_report
from open_source_intelligence.reports.errors import ReportGenerationError


@dataclass(slots=True)
class DraftedRun:
    """一次已完成的 Agent 运行及其落库的草稿。"""

    agent_run_id: int
    draft_id: int


@pytest.fixture
def drafted_run(session: Session, workspace: Workspace) -> Iterator[DraftedRun]:
    """通过真实 Agent 运行时生成一份确定性的草稿。"""
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
    """证明一份合法草稿会变成一份报告，模板章节与来源行齐全。"""
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
    """证明重复跑同一份草稿不会造出重复的版本或报告。"""
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
    """证明草稿一改就生成一个新的、不可变的报告版本。"""
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
    """证明没有证据的章节不得被晋升进报告。"""
    section = _latest_section(session, drafted_run.draft_id, "summary")
    section.citations_json = []
    session.commit()

    with pytest.raises(ReportGenerationError, match="has no citations"):
        generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)


def test_invalid_metric_reference_is_rejected_before_write(
    session: Session,
    drafted_run: DraftedRun,
) -> None:
    """证明伪造的指标引用会失败，且不会留下半份报告。"""
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
    """证明已发布内容不可变，除非走显式的复核流程来改它。"""
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
    """证明已发布的历史报告才能解析成功；草稿不能变成引用。"""
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
    """证明生成的报告按稳定的 `owner/name` 引用仓库，而不是用自增 id。"""
    result = generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)
    report = session.get(Report, result.report_id)
    assert report is not None

    sources = session.scalars(select(ReportSource).where(ReportSource.report_id == report.id)).all()
    metric_refs = [s.source_ref for s in sources if s.source_type == "metric"]
    assert metric_refs
    # 现在每条指标引用都锚在全名（含 `/`）上，而不是一个光秃秃的 id。
    assert all("/" in ref.split(":")[1] for ref in metric_refs)
    # 渲染出来的报告正文里也是同一种稳定写法。
    for ref in metric_refs:
        assert ref in report.content_markdown


def test_full_name_citation_resolves_after_the_id_changes(
    session: Session,
    drafted_run: DraftedRun,
) -> None:
    """证明即使仓库 id 不是起草时的键，按全名给出的引用也能解析。"""
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
    """证明报告正文带一块来源说明：候选、窗口、被引用的快照数。"""
    result = generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)
    report = session.get(Report, result.report_id)
    assert report is not None
    assert "本轮候选仓库：" in report.content_markdown
    assert "快照窗口：" in report.content_markdown
    assert "引用快照" in report.content_markdown


def test_evidence_section_deduplicates_and_groups_sources(
    session: Session,
    drafted_run: DraftedRun,
) -> None:
    """证明被多个章节引用的同一份快照，在证据区只出现一次。"""
    result = generate_report(session, agent_run_id=drafted_run.agent_run_id, now=NOW)
    report = session.get(Report, result.report_id)
    assert report is not None

    evidence = report.content_markdown.split("## 引用与证据", 1)[1]
    # 归在指标小标题之下。
    assert "### 指标快照" in evidence
    # 证据块里任何一个 `source_ref` 都不该出现两次。
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
    """证明来源行描述的是这次运行的输入，而不是引用清单。

    有两个数字错在同一个方向上：「指标观测点」数的是 `(章节, 引用)` 对，于是一份被三节
    引用的快照会读成三次观测；「本轮候选仓库」则从被引用的仓库推出来，于是一个研究过、
    只是没被引用的候选，就从「这次运行看得到什么」的清单里消失了。

    这一行还得不再叫观测数：正文另外报的是每条*序列覆盖*多少份快照（比如四份），于是
    同一个词承载了两个总数，报告自相矛盾 —— 「引用快照 3 个」与「覆盖 4 个快照」是两个
    不同且都正确的量。
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
    assert f"引用快照 {len(metric_refs)} 个" in body

    # 候选链接是这次运行的输入；无论是否被引用，这一块都得把它们全写出来。
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
    """证明全名本身并不唯一，所以由日期决定这条引用指的是哪个来源。

    `repositories` 只在 `(source_id, provider_repo_id)` 上唯一，因此两个数据源可以持有
    同一个 `owner/name`。先按名字解析、再指望这一行恰好拥有那份快照，会让答案取决于
    插入顺序：一条明明点明了第二个来源快照的引用，会被判成「找不到」。
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


def test_the_key_numbers_table_is_computed_not_drafted() -> None:
    """证明份额这一列是算出来的，因此由数据行渲染，而不是向模型索取。

    一次真实运行把 92,667 中的 84,713 报成了 64.8%。读者不重算一遍除法，就看不出一个
    百分比是错还是对，而这正是报告不该从语言模型那里拿的那类数字。
    """

    @dataclass
    class _Section:
        content_markdown: str

    sections = {key: _Section(content_markdown="正文") for key in REPORT_SECTION_KEYS}
    markdown = _compose_from_resolved(
        "标题",
        sections,  # type: ignore[arg-type]
        {},
        window_start=NOW,
        window_end=NOW,
        candidate_full_names=("example/a", "example/b"),
        movements=(("example/a", 300), ("example/b", 3)),
    )

    assert "关键数字（系统统计）" in markdown
    assert "| example/a | +300 | 99.0% |" in markdown
    assert "| example/b | +3 | 1.0% |" in markdown
    assert "| **合计** | **+303** | 100% |" in markdown
