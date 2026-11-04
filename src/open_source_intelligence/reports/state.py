"""实现报告发布状态机，集中定义合法的状态迁移。

`reports.status` 有三处写入方 —— 发布策略引擎、人工修订服务、报告编排 —— 三条路径各写
各的，合法的迁移边就必须收在一个独立模块里，否则三份拷贝会一直看起来一致，直到有人只
改好了其中一份。放在这里同时也避开了循环导入：`publishing` 依赖 `verification`，
`verification` 依赖 `composition`，所以 `composition` 不能再反向导入 `publishing`。
"""

from __future__ import annotations

from typing import Final

from open_source_intelligence.persistence.models.reporting import Report

from .errors import ReportStateError

#: 合法的报告状态迁移。`failed` 是终态；`published` 只能被拉回 `draft`，撤稿或回滚
#: 正是靠这条边把报告送回队列。
ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "draft": frozenset({"pending_review", "scheduled", "failed"}),
    "pending_review": frozenset({"approved", "changes_requested", "failed"}),
    "approved": frozenset({"scheduled", "failed"}),
    "scheduled": frozenset({"published", "failed"}),
    "changes_requested": frozenset({"draft"}),
    "published": frozenset({"draft"}),
    "failed": frozenset(),
}


def transition_report(report: Report, target: str) -> None:
    """把报告迁移到 `target`，拒绝状态机禁止的任何一条边。"""
    if report.status == target:
        return
    allowed = ALLOWED_TRANSITIONS.get(report.status, frozenset())
    if target not in allowed:
        raise ReportStateError(f"report {report.id} cannot move from {report.status} to {target}")
    report.status = target
