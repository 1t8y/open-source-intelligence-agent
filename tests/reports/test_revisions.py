"""手动编辑与回滚：状态守卫、版本记账，以及引用拷贝。

`tests/web/test_web.py` 会经由 UI 驱动这些服务，但只从 `published` 起步。
守卫本身一直没有用例钉住，`scheduled` 状态下的回滚正是因此溜了过去，在状态机
拒绝这次迁移之前就已经改动了报告。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from reports_support import AUTO, SeededReport, build_report
from sqlalchemy import select
from sqlalchemy.orm import Session
from support import NOW, Workspace

from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportSource,
    ReportVersion,
)
from open_source_intelligence.reports.errors import (
    ReportGenerationError,
    ReportNotFoundError,
    ReportStateError,
)
from open_source_intelligence.reports.revisions import (
    EDITABLE_STATUSES,
    ROLLBACKABLE_STATUSES,
    revise_report,
    rollback_report,
)
from open_source_intelligence.reports.state import ALLOWED_TRANSITIONS, transition_report
from open_source_intelligence.reports.verification import verify_report

#: 报告可能停留、且必须拒绝回滚的状态，以及抵达每一个的合法路径。`failed` 单独
#: 覆盖，因为它是终态分支。
PUBLISHING_PATH = (
    ("pending_review", ("pending_review",)),
    ("approved", ("pending_review", "approved")),
    ("scheduled", ("scheduled",)),
)


def _advance(session: Session, report_id: int, *targets: str) -> Report:
    """让报告走一遍真实的状态机，并返回那一行。"""
    report = session.get(Report, report_id)
    assert report is not None
    for target in targets:
        transition_report(report, target)
    session.commit()
    return report


def _revise(session: Session, case: SeededReport) -> Report:
    """做一次正文修改，让报告带上第二个版本。"""
    report = case.reload(session)
    revise_report(
        session,
        report_id=case.report_id,
        content_markdown=report.content_markdown + "\n\n补充说明\n",
        now=NOW + timedelta(minutes=1),
    )
    return case.reload(session)


def _version_rows(session: Session, report_id: int) -> int:
    """返回某份报告已落库的版本数。"""
    return len(
        session.scalars(select(ReportVersion).where(ReportVersion.report_id == report_id)).all()
    )


def test_rollbackable_statuses_match_the_state_machine() -> None:
    """声明的集合必须等于「合法迁移中含 `draft`」的那些状态。"""
    derived = {status for status, targets in ALLOWED_TRANSITIONS.items() if "draft" in targets}
    derived.add("draft")  # `transition_report` 把 `draft` -> `draft` 视为空操作
    assert ROLLBACKABLE_STATUSES == frozenset(derived)
    assert "failed" not in ROLLBACKABLE_STATUSES


def test_editable_statuses_are_the_draft_queue() -> None:
    """只有草稿队列里的报告可以手工改写。"""
    assert EDITABLE_STATUSES == frozenset({"draft", "changes_requested"})
    assert EDITABLE_STATUSES <= ROLLBACKABLE_STATUSES


@pytest.mark.parametrize(("label", "targets"), PUBLISHING_PATH)
def test_rollback_is_refused_without_touching_the_report(
    session: Session,
    workspace: Workspace,
    label: str,
    targets: tuple[str, ...],
) -> None:
    """被拒绝的回滚，对象与数据库都必须原封不动。

    这是顺序缺陷的回归用例：服务此前先追加版本、最后才问状态机，于是拒绝来得太迟
    —— `current_version` 已经被抬高，正文也已经在 UI 正在渲染的那个对象上
    被替换。
    """
    case = build_report(session, workspace, publish_mode=AUTO)
    _revise(session, case)
    report = _advance(session, case.report_id, *targets)
    assert report.status == label

    before_version = report.current_version
    before_content = report.content_markdown
    before_rows = _version_rows(session, case.report_id)

    with pytest.raises(ReportStateError):
        rollback_report(session, report_id=case.report_id, version=1, now=NOW)

    # 断言的是服务改动过的那个同一性映射对象；刻意不重新查询，因为错误被捕获
    # 之后 UI 面板渲染的正是这个对象。
    assert report.status == label
    assert report.current_version == before_version
    assert report.content_markdown == before_content
    assert _version_rows(session, case.report_id) == before_rows


def test_rollback_refuses_the_terminal_failed_status(
    session: Session,
    workspace: Workspace,
) -> None:
    """`failed` 是终态：被驳回的报告永远不会靠回滚复活。"""
    case = build_report(session, workspace, publish_mode=AUTO)
    report = _revise(session, case)
    _advance(session, case.report_id, "failed")
    before_version = report.current_version

    with pytest.raises(ReportStateError):
        rollback_report(session, report_id=case.report_id, version=1, now=NOW)

    assert report.status == "failed"
    assert report.current_version == before_version


def test_rollback_from_draft_restores_an_earlier_version(
    session: Session,
    workspace: Workspace,
) -> None:
    """把草稿回退会追加一个新版本，并拷贝目标版本的引用。"""
    case = build_report(session, workspace, publish_mode=AUTO)
    report = _revise(session, case)
    before_version = report.current_version
    first_content = session.scalar(
        select(ReportVersion.content_markdown).where(
            ReportVersion.report_id == case.report_id,
            ReportVersion.version == 1,
        )
    )
    assert first_content is not None
    source_rows_before = len(
        session.scalars(
            select(ReportSource).where(
                ReportSource.report_id == case.report_id,
                ReportSource.version == 1,
            )
        ).all()
    )

    outcome = rollback_report(session, report_id=case.report_id, version=1, now=NOW)

    assert outcome.restored_from == 1
    assert outcome.version == before_version + 1
    assert outcome.status == "draft"
    assert report.content_markdown == first_content
    assert report.published_at is None
    assert report.publish_at is None
    # 恢复出来的版本带着它自己的一份引用集合。
    restored = session.scalars(
        select(ReportSource).where(
            ReportSource.report_id == case.report_id,
            ReportSource.version == outcome.version,
        )
    ).all()
    assert len(restored) == source_rows_before


def test_rollback_from_published_withdraws_the_report(
    session: Session,
    workspace: Workspace,
) -> None:
    """已发布的报告可以被拉回 `draft`。"""
    case = build_report(session, workspace, publish_mode=AUTO)
    _revise(session, case)
    _advance(session, case.report_id, "scheduled", "published")

    outcome = rollback_report(session, report_id=case.report_id, version=1, now=NOW)

    assert outcome.status == "draft"
    report = case.reload(session)
    assert report.published_at is None


def test_rollback_rejects_the_current_version(
    session: Session,
    workspace: Workspace,
) -> None:
    """回滚到当前已在的版本，不算一次修订。"""
    case = build_report(session, workspace, publish_mode=AUTO)
    report = _revise(session, case)
    with pytest.raises(ReportGenerationError):
        rollback_report(session, report_id=case.report_id, version=report.current_version)


def test_rollback_rejects_an_unknown_version(
    session: Session,
    workspace: Workspace,
) -> None:
    """从未写入过的版本无从恢复。"""
    case = build_report(session, workspace, publish_mode=AUTO)
    with pytest.raises(ReportNotFoundError):
        rollback_report(session, report_id=case.report_id, version=99)


def test_content_edit_appends_a_version_and_returns_to_draft(
    session: Session,
    workspace: Workspace,
) -> None:
    """正文一变就不可变，因此它追加新版本并重回草稿队列。"""
    case = build_report(session, workspace, publish_mode=AUTO)
    report = case.reload(session)
    original = report.content_markdown

    outcome = revise_report(
        session,
        report_id=case.report_id,
        content_markdown=original + "\n\n新增段落\n",
        now=NOW,
    )

    assert outcome.content_changed is True
    assert outcome.version == 2
    assert outcome.status == "draft"
    assert _version_rows(session, case.report_id) == 2


def test_metadata_edit_does_not_append_a_version(
    session: Session,
    workspace: Workspace,
) -> None:
    """只改元数据时，当前版本与状态都保持不变。"""
    case = build_report(session, workspace, publish_mode=AUTO)
    report = case.reload(session)
    before_version = report.current_version

    outcome = revise_report(
        session,
        report_id=case.report_id,
        visibility="PRIVATE",
        now=NOW,
    )

    assert outcome.content_changed is False
    assert outcome.version == before_version
    assert outcome.changed_fields == ("visibility",)
    assert report.visibility == "PRIVATE"
    assert report.owner_id is not None


def test_edit_is_refused_outside_the_draft_queue(
    session: Session,
    workspace: Workspace,
) -> None:
    """已经走上发布流程的报告不能被手工改写。"""
    case = build_report(session, workspace, publish_mode=AUTO)
    _advance(session, case.report_id, "scheduled")

    with pytest.raises(ReportStateError):
        revise_report(session, report_id=case.report_id, title="改标题", now=NOW)


def test_edit_refuses_an_unknown_visibility(
    session: Session,
    workspace: Workspace,
) -> None:
    """可见性是封闭集合，未知取值根本到不了那一列。"""
    case = build_report(session, workspace, publish_mode=AUTO)
    with pytest.raises(ReportGenerationError):
        revise_report(session, report_id=case.report_id, visibility="SECRET", now=NOW)


@pytest.mark.parametrize("bad_title", ["", "   ", "第一行\n第二行", "x" * 256])
def test_edit_refuses_a_title_that_cannot_render_as_one_heading(
    session: Session,
    workspace: Workspace,
    bad_title: str,
) -> None:
    """空白、多行或过长的标题会被拒绝，而不是存进去。

    结构检查会拿正文第一行比对 `# <title>`，因此标题里一旦有换行，报告就会卡在
    校验里，而失败原因看起来却像是正文出了问题。
    """
    case = build_report(session, workspace, publish_mode=AUTO)
    report = case.reload(session)
    before_title = report.title

    with pytest.raises(ReportGenerationError):
        revise_report(session, report_id=case.report_id, title=bad_title, now=NOW)

    assert report.title == before_title


def test_edit_trims_a_title(session: Session, workspace: Workspace) -> None:
    """首尾空白会被规范化，而不是原样落库。"""
    case = build_report(session, workspace, publish_mode=AUTO)
    before_version = case.reload(session).current_version

    outcome = revise_report(session, report_id=case.report_id, title="  新标题  ", now=NOW)

    report = case.reload(session)
    assert report.title == "新标题"
    # 标题就是正文的第一个标题行，所以改名同时也是一次正文改动：若把旧的 `# ...`
    # 留在原处，结构检查会永远拒绝这份报告。
    assert outcome.changed_fields == ("title", "content_markdown")
    assert report.content_markdown.splitlines()[0] == "# 新标题"
    assert outcome.version == before_version + 1


def test_rename_keeps_the_report_verifiable(session: Session, workspace: Workspace) -> None:
    """改过名的报告仍能通过结构检查，因此仍可发布。"""
    case = build_report(session, workspace, publish_mode=AUTO)
    revise_report(session, report_id=case.report_id, title="改过的标题", now=NOW)

    report = case.reload(session)
    verdict = verify_report(session, report_id=report.id, now=NOW + timedelta(minutes=1))

    assert verdict.structure_ok is True
    assert report.content_markdown.splitlines()[0] == "# 改过的标题"
