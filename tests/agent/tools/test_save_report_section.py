"""守护 `save_report_section` 的用例：草稿创建、幂等与版本。"""

from __future__ import annotations

from sqlalchemy import func, select
from support import Workspace

from open_source_intelligence.agent.tools.contracts import TOOL_INVALID_ARGUMENT
from open_source_intelligence.agent.tools.registry import build_default_registry
from open_source_intelligence.persistence.models.reporting import ReportDraft, ReportSection


def _seed(workspace: Workspace):
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    return topic, run


def _section_count(session) -> int:
    return session.scalar(select(func.count()).select_from(ReportSection)) or 0


def test_first_save_creates_draft_and_version_one(workspace: Workspace) -> None:
    """证明保存章节会为这次运行创建草稿并返回版本 1。"""
    topic, run = _seed(workspace)
    registry = build_default_registry()

    result = registry.invoke(
        "save_report_section",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {
            "section_key": "summary",
            "content_markdown": "## Summary\nAgents are trending up.",
            "content_digest": "digest-0001",
            "citations": [{"source_type": "metric", "source_ref": "repo:1#stars"}],
        },
    )

    assert result.ok is True
    ref = result.data
    assert ref is not None
    assert ref.version == 1
    assert ref.section_key == "summary"
    drafts = workspace.session.scalars(select(ReportDraft)).all()
    assert len(drafts) == 1
    assert drafts[0].id == ref.report_draft_id
    section = workspace.session.scalars(select(ReportSection)).one()
    assert section.citations_json == [
        {"source_type": "metric", "source_ref": "repo:1#stars", "source_url": None}
    ]


def test_identical_content_is_idempotent(workspace: Workspace) -> None:
    """证明同样的摘要再存一次返回已有版本，不新增行。"""
    topic, run = _seed(workspace)
    registry = build_default_registry()
    ctx = workspace.context(topic.id, agent_run_id=run.id)
    arguments = {
        "section_key": "summary",
        "content_markdown": "same content",
        "content_digest": "digest-same",
    }

    first = registry.invoke("save_report_section", ctx, workspace.session, arguments)
    count_after_first = _section_count(workspace.session)
    second = registry.invoke("save_report_section", ctx, workspace.session, arguments)

    assert first.data == second.data
    assert _section_count(workspace.session) == count_after_first == 1


def test_changed_content_bumps_the_version(workspace: Workspace) -> None:
    """证明同一章节的新内容会生成下一个版本。"""
    topic, run = _seed(workspace)
    registry = build_default_registry()
    ctx = workspace.context(topic.id, agent_run_id=run.id)

    first = registry.invoke(
        "save_report_section",
        ctx,
        workspace.session,
        {"section_key": "trend", "content_markdown": "v1", "content_digest": "digest-v1"},
    )
    second = registry.invoke(
        "save_report_section",
        ctx,
        workspace.session,
        {"section_key": "trend", "content_markdown": "v2", "content_digest": "digest-v2"},
    )

    assert first.data is not None and first.data.version == 1
    assert second.data is not None and second.data.version == 2
    assert first.data.report_draft_id == second.data.report_draft_id
    assert _section_count(workspace.session) == 2


def test_distinct_sections_share_one_draft(workspace: Workspace) -> None:
    """证明不同的章节键共用同一份「每次运行一份」的草稿。"""
    topic, run = _seed(workspace)
    registry = build_default_registry()
    ctx = workspace.context(topic.id, agent_run_id=run.id)

    summary = registry.invoke(
        "save_report_section",
        ctx,
        workspace.session,
        {"section_key": "summary", "content_markdown": "s", "content_digest": "digest-s"},
    )
    risks = registry.invoke(
        "save_report_section",
        ctx,
        workspace.session,
        {"section_key": "risks", "content_markdown": "r", "content_digest": "digest-r"},
    )

    assert summary.data is not None and risks.data is not None
    assert summary.data.report_draft_id == risks.data.report_draft_id
    assert summary.data.version == 1
    assert risks.data.version == 1
    assert workspace.session.scalar(select(func.count()).select_from(ReportDraft)) == 1


def test_unknown_section_key_is_rejected(workspace: Workspace) -> None:
    """证明只允许写入模板章节。"""
    topic, run = _seed(workspace)
    registry = build_default_registry()

    result = registry.invoke(
        "save_report_section",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {
            "section_key": "freeform",
            "content_markdown": "text",
            "content_digest": "digest-xyz",
        },
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == TOOL_INVALID_ARGUMENT
    assert _section_count(workspace.session) == 0
