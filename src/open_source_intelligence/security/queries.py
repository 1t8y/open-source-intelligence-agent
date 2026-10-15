"""Read-side queries behind the audit log API.

The audit log is append-only, and this module is the read half of that arrangement: there is
no update or delete counterpart anywhere in the codebase. `docs/api-contract.md` §8 states the
constraint by *not* providing endpoints rather than by gating them with a role, which is the
stronger form — a permission cannot be misconfigured into existence.

`metadata_json` is deliberately not selected here. It is a free-form field whose allowed keys
have never been defined, so putting it on an egress path would be shipping an undefined
contract; when it is needed, the keys should be enumerated first.
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
    """One page of audit events plus the total matching the filters."""

    items: tuple[AuditLog, ...]
    page: int
    page_size: int
    total: int


def clamp_page_size(page_size: int) -> int:
    """Keep a requested page size inside the documented 1-100 range."""
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
    """Return one page of audit events, newest first.

    The window is half-open (`created_after <= created_at < created_before`), matching the
    convention the monitoring overview uses.

    "Newest first" is `id DESC`, not `created_at DESC`, and the contract §8 says the same. `id`
    is monotonic on an append-only table and is a *total* order, which `created_at` is not:
    several rows can share a timestamp, and a page boundary falling inside such a group lets the
    database return the group in either arrangement, so a row can appear on two pages or on none
    (the same reason `pipeline/queries.py` always appends `id` as a tiebreaker). Ordering by a
    column that can tie is what makes an otherwise stable read unstable.
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
