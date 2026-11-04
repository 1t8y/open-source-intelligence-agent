"""审计日志 API 背后的读侧查询。

审计日志只追加，而本模块是这种安排中读的那一半：整个代码库里都没有对应的更新或
删除实现。`docs/api-contract.md` §8 表达这条约束的方式是*不提供*端点，而不是用角色
去把关，后者是更强的形式 —— 权限无法被误配置成存在。

这里刻意不查询 `metadata_json`。它是一个自由格式字段，允许哪些键从未定义过，把它放到
对外输出路径上等于交付一份未定义的契约；真需要它时，应该先把键枚举出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.audit import AuditLog

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class AuditLogPage:
    """一页审计事件，以及符合筛选条件的总数。"""

    items: tuple[AuditLog, ...]
    page: int
    page_size: int
    total: int


def clamp_page_size(page_size: int) -> int:
    """把请求的分页大小限制在文档约定的 1-100 范围内。"""
    return max(1, min(page_size, MAX_PAGE_SIZE))


def list_audit_logs(
    session: Session,
    *,
    actor_id: int | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    result: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> AuditLogPage:
    """返回一页审计事件，最新的在前。

    时间窗是半开区间（`created_after <= created_at < created_before`），与监控总览
    使用的约定一致。

    「最新在前」用的是 `id DESC`，而不是 `created_at DESC`，契约 §8 也是这么写
    的。`id` 在只追加的表上单调递增，是一种*全序*，而 `created_at` 不是：多行可以
    共享同一个时间戳，一旦分页边界落在这样的组里，数据库就可以按任意顺序返回这组
    行，于是一行可能出现在两页上，也可能一页都不出现（这也是
    `pipeline/queries.py` 总是追加 `id` 作为次序打破者的原因）。按一个可能打平的
    列排序，正是让一次本来稳定的读取变得不稳定的原因。
    """
    resolved_page = max(1, page)
    resolved_size = clamp_page_size(page_size)
    conditions: list[ColumnElement[bool]] = []
    if actor_id is not None:
        conditions.append(AuditLog.actor_id == actor_id)
    if action is not None:
        conditions.append(AuditLog.action == action)
    if resource_type is not None:
        conditions.append(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        conditions.append(AuditLog.resource_id == resource_id)
    if result is not None:
        conditions.append(AuditLog.result == result)
    if created_after is not None:
        conditions.append(AuditLog.created_at >= created_after)
    if created_before is not None:
        conditions.append(AuditLog.created_at < created_before)

    total = int(session.scalar(select(func.count()).select_from(AuditLog).where(*conditions)) or 0)
    items = session.scalars(
        select(AuditLog)
        .where(*conditions)
        .order_by(AuditLog.id.desc())
        .offset((resolved_page - 1) * resolved_size)
        .limit(resolved_size)
    ).all()
    return AuditLogPage(
        items=tuple(items),
        page=resolved_page,
        page_size=resolved_size,
        total=total,
    )
