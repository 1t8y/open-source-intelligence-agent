"""执行报告修订操作：人工编辑，以及回滚到历史版本。

两种操作都会追加一行不可变的 `report_versions`，并且（在内容发生变化时）把报告退回
`draft`，因为内容被改过的产物必须重新验证、重新发布，才能让任何人看到。两者的区别只在
于内容从哪来：编辑的内容来自调用方，回滚的内容来自一个已经放行过一次的版本。

每个新版本都继承它所替代那一版的引用集合。这让编辑过的报告仍然有资格发布，同时 `INV-10`
依然可执行：验证器会把每一条继承来的引用重新对着数据库解析，所以一条断掉的引用、或者
一个写了数字却没有指标引用的章节，仍然会拦下发布。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.reporting import (
    REPORT_SENSITIVITIES,
    REPORT_VISIBILITIES,
    Report,
    ReportSource,
    ReportVersion,
)
from open_source_intelligence.security.audit import record_audit_event

from .errors import ReportGenerationError, ReportNotFoundError, ReportStateError
from .state import transition_report

#: 允许人工改写报告内容的状态。这条规则与 `generate_report` 已经在用的规则一致，
#: 两条编辑路径对「报告何时可编辑」才不会有分歧。
EDITABLE_STATUSES: Final[frozenset[str]] = frozenset({"draft", "changes_requested"})

#: 编辑路径接受的最长标题；与 `Report.title` 的列宽一致。
MAX_TITLE_CHARS: Final[int] = 255

#: 允许回滚并返回 `draft` 的状态。它对应通向 `draft` 的合法边：`draft` 与
#: `changes_requested` 可以倒回更早的版本，`published` 则可以撤稿。仍在发布路径上的
#: 报告（`pending_review`、`approved`、`scheduled`）必须先显式离开那条路径，而
#: `failed` 是终态。
ROLLBACKABLE_STATUSES: Final[frozenset[str]] = frozenset(
    {"draft", "changes_requested", "published"}
)


class _Unset:
    """哨兵类型，用来把 `publish_at=None` 与「保持不变」区分开。"""


_UNSET = _Unset()


@dataclass(frozen=True, slots=True)
class RevisionOutcome:
    """一次编辑或回滚的结果。"""

    report_id: int
    version: int
    status: str
    content_changed: bool
    changed_fields: tuple[str, ...]
    restored_from: int | None

    def to_dict(self) -> dict[str, Any]:
        """返回可安全转 JSON 的载荷。"""
        return {
            "report_id": self.report_id,
            "version": self.version,
            "status": self.status,
            "content_changed": self.content_changed,
            "changed_fields": list(self.changed_fields),
            "restored_from": self.restored_from,
        }


def _require_report(session: Session, report_id: int) -> Report:
    """加载一份报告，失败时抛出带稳定错误码的异常。"""
    report = session.get(Report, report_id)
    if report is None:
        raise ReportNotFoundError(f"report {report_id} does not exist")
    return report


def _normalize_title(title: str) -> str:
    """返回去掉首尾空白的标题，并拒绝任何无法渲染成单个标题的内容。

    `verification._check_structure` 会把正文第一行与 `# <title>` 做比较，所以带换行的
    标题永远过不了结构检查：报告会卡在验证 `failed` 上、无路可发，而且理由看起来像内容
    问题，而不是某个字段填错了。
    """
    normalized = title.strip()
    if (
        not normalized
        or len(normalized) > MAX_TITLE_CHARS
        or "\n" in normalized
        or "\r" in normalized
    ):
        raise ReportGenerationError(
            f"title must be a single non-empty line of at most {MAX_TITLE_CHARS} characters"
        )
    return normalized


def _with_title_heading(content: str, title: str) -> str:
    """返回 `content`，并把开头的 `# ` 标题行改成 `title`。

    `verification._check_structure` 会把正文第一行与 `# <title>` 做比较，所以标题与
    正文标题行是同一个值的两份存储。任何改动其中之一的编辑路径都必须同步改另一个，
    否则产出的报告结构检查永远过不去 —— 一个看起来像草稿太糟、其实是编辑不当的内容
    问题。
    """
    desired = f"# {title}"
    lines = content.splitlines()
    if lines and lines[0].startswith("# "):
        if lines[0].rstrip() == desired:
            return content
        lines[0] = desired
        return "\n".join(lines)
    stripped = content.lstrip()
    if not stripped:
        return desired + "\n"
    return f"{desired}\n\n{stripped}"


def _copy_sources(
    session: Session,
    report: Report,
    *,
    from_version: int,
    to_version: int,
    moment: datetime,
) -> int:
    """把一个版本的引用集合复制到另一个版本，并返回搬动了几行。"""
    session.execute(
        delete(ReportSource).where(
            ReportSource.report_id == report.id,
            ReportSource.version == to_version,
        )
    )
    rows = session.scalars(
        select(ReportSource).where(
            ReportSource.report_id == report.id,
            ReportSource.version == from_version,
        )
    ).all()
    for row in rows:
        session.add(
            ReportSource(
                report_id=report.id,
                version=to_version,
                repository_id=row.repository_id,
                section_key=row.section_key,
                claim_text=row.claim_text,
                source_type=row.source_type,
                source_url=row.source_url,
                source_ref=row.source_ref,
                metric_snapshot_id=row.metric_snapshot_id,
                created_at=moment,
            )
        )
    return len(rows)


def _append_version(
    session: Session,
    report: Report,
    *,
    content: str,
    actor_id: int | None,
    change_reason: str,
    moment: datetime,
) -> int:
    """追加下一个不可变版本，并返回它的版本号。"""
    version = report.current_version + 1
    report.current_version = version
    report.content_markdown = content
    session.add(
        ReportVersion(
            report_id=report.id,
            version=version,
            content_markdown=content,
            created_by=actor_id,
            change_reason=change_reason,
            created_at=moment,
        )
    )
    return version


def _return_to_draft(report: Report) -> None:
    """把报告放回编辑队列，并清掉它因此失去的发布窗口。"""
    transition_report(report, "draft")
    report.publish_at = None
    report.published_at = None


def revise_report(
    session: Session,
    *,
    report_id: int,
    actor_id: int | None = None,
    title: str | None = None,
    content_markdown: str | None = None,
    visibility: str | None = None,
    sensitivity: str | None = None,
    publish_at: datetime | None | _Unset = _UNSET,
    now: datetime | None = None,
) -> RevisionOutcome:
    """套用一次人工编辑；内容发生变化时追加一个版本。

    只改元数据的编辑就地更新该行；内容编辑在定义上就是不可变的，因此它会追加一个版本，
    并把报告退回 `draft` 以便重新验证。
    """
    moment = now or utc_now()
    report = _require_report(session, report_id)
    if report.status not in EDITABLE_STATUSES:
        raise ReportStateError(
            f"report {report.id} is {report.status}; only a draft or a report returned for "
            "changes can be edited"
        )
    if visibility is not None and visibility not in REPORT_VISIBILITIES:
        raise ReportGenerationError(f"unsupported visibility: {visibility}")
    if sensitivity is not None and sensitivity not in REPORT_SENSITIVITIES:
        raise ReportGenerationError(f"unsupported sensitivity: {sensitivity}")
    if not isinstance(publish_at, _Unset) and publish_at is not None and publish_at.tzinfo is None:
        raise ReportGenerationError("publish_at must be a timezone-aware UTC timestamp")
    if title is not None:
        title = _normalize_title(title)

    changed: list[str] = []
    resulting_visibility = visibility if visibility is not None else report.visibility
    if resulting_visibility == "PRIVATE" and report.owner_id is None:
        if actor_id is None:
            raise ReportGenerationError("a PRIVATE report requires a recorded owner")
        report.owner_id = actor_id
        changed.append("owner_id")
    if title is not None and title != report.title:
        report.title = title
        changed.append("title")
    if visibility is not None and visibility != report.visibility:
        report.visibility = visibility
        changed.append("visibility")
    if sensitivity is not None and sensitivity != report.sensitivity:
        report.sensitivity = sensitivity
        changed.append("sensitivity")
    if not isinstance(publish_at, _Unset) and publish_at != report.publish_at:
        report.publish_at = publish_at
        changed.append("publish_at")

    # 标题会被渲染成正文的第一个标题行，所以两者必须一起动。只改标题的编辑因此会变成
    # 一次内容编辑（并追加一个版本）；而在新标题下重写正文的编辑，会先把标题行规范化，
    # 而不是等到后面结构检查失败。两条路径都让 `report.title` 与 `content_markdown`
    # 描述同一份文档。
    if content_markdown is None and "title" in changed:
        content_markdown = _with_title_heading(report.content_markdown, report.title)
    elif content_markdown is not None:
        content_markdown = _with_title_heading(content_markdown, report.title)

    content_changed = content_markdown is not None and content_markdown != report.content_markdown
    version = report.current_version
    if content_changed:
        assert content_markdown is not None
        previous_version = report.current_version
        # 在新版本写入之前先把报告退回 `draft`，与 `rollback_report` 保持一致：允许编辑
        # 的状态恰好就是能进入 `draft` 的状态，先做这个决定，被拒绝的迁移就不会留下一个
        # 改了一半的修订。
        _return_to_draft(report)
        version = _append_version(
            session,
            report,
            content=content_markdown,
            actor_id=actor_id,
            change_reason="manual_edit",
            moment=moment,
        )
        _copy_sources(
            session,
            report,
            from_version=previous_version,
            to_version=version,
            moment=moment,
        )
        changed.append("content_markdown")

    if changed:
        record_audit_event(
            session,
            action="report.edit",
            resource_type="report",
            resource_id=str(report.id),
            result="success",
            actor_id=actor_id,
            metadata_json={
                "version": version,
                "changed_fields": changed,
                "content_changed": content_changed,
            },
        )
    session.commit()
    return RevisionOutcome(
        report_id=report.id,
        version=version,
        status=report.status,
        content_changed=content_changed,
        changed_fields=tuple(changed),
        restored_from=None,
    )


def rollback_report(
    session: Session,
    *,
    report_id: int,
    version: int,
    actor_id: int | None = None,
    comment: str | None = None,
    now: datetime | None = None,
) -> RevisionOutcome:
    """把一个历史版本恢复成一个新的草稿版本。

    被恢复的内容及其引用集合会被复制到一个*新*版本上，而不是把 `current_version` 倒
    回去，这样版本历史保持只追加，报告也仍然必须重新通过验证和发布策略。
    """
    moment = now or utc_now()
    report = _require_report(session, report_id)
    # 状态检查在任何写入之前。`_return_to_draft` 可能拒绝这次迁移，而拒绝必须让报告
    # 原封不动：事后再检查，等于一边把错误抛给调用方，一边让会话里已经躺着改了一半的
    # 修订 —— 被加一的 `current_version`、被替换的正文、暂存的 `report_versions` /
    # `report_sources` 行 —— 而 UI 随后就会在同一个 identity map 对象上把它们渲染
    # 出来。
    if report.status not in ROLLBACKABLE_STATUSES:
        raise ReportStateError(
            f"report {report.id} is {report.status}; only a draft, a report returned for "
            "changes, or a published report can be rolled back"
        )
    target = session.scalar(
        select(ReportVersion).where(
            ReportVersion.report_id == report.id,
            ReportVersion.version == version,
        )
    )
    if target is None:
        raise ReportNotFoundError(f"report {report.id} has no version {version}")
    if version == report.current_version:
        raise ReportGenerationError(f"version {version} is already the current version")

    previous_version = report.current_version
    # 先推进状态机，理由与上面的检查相同：一旦版本行存在，修订就已经发生了，所以迁移
    # 必须在它之前决定。
    _return_to_draft(report)
    new_version = _append_version(
        session,
        report,
        # `reports.title` 不参与版本化，所以恢复出来的正文要重新挂到报告当前携带的标题
        # 上。否则，跨过一次改名的回滚会恢复出结构检查在下一轮验证时就拒绝的内容。
        content=_with_title_heading(target.content_markdown, report.title),
        actor_id=actor_id,
        change_reason=f"rollback_to_v{version}",
        moment=moment,
    )
    restored_sources = _copy_sources(
        session,
        report,
        from_version=version,
        to_version=new_version,
        moment=moment,
    )
    record_audit_event(
        session,
        action="report.rollback",
        resource_type="report",
        resource_id=str(report.id),
        result="success",
        actor_id=actor_id,
        metadata_json={
            "restored_version": version,
            "from_version": previous_version,
            "to_version": new_version,
            "restored_sources": restored_sources,
            "comment": comment,
        },
    )
    session.commit()
    return RevisionOutcome(
        report_id=report.id,
        version=new_version,
        status=report.status,
        content_changed=True,
        changed_fields=("content_markdown",),
        restored_from=version,
    )
