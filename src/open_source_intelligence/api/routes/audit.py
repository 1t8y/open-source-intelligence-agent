"""审计日志端点：在只追加的轨迹之上提供只读视图。

`docs/api-contract.md` §8 固定了响应形状。这个端点是现有意义上最强的只读 —— 没有更新或删除
路由需要保护，因此任何配置失误都开不出一扇写的门。
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, get_args

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.security.audit import format_ip_address
from open_source_intelligence.security.authorization import AUDIT_READ_ROLES
from open_source_intelligence.security.dependencies import get_db_session, require_roles
from open_source_intelligence.security.queries import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    list_audit_logs,
)

from ..params import UtcDateTime

router = APIRouter(prefix="/api/v1/audit-logs", tags=["audit"])

AuditUser = Annotated[User, Depends(require_roles(*AUDIT_READ_ROLES))]
DbSession = Annotated[Session, Depends(get_db_session)]

#: `audit_logs.result` 允许的三种结果。列表从注解派生，而不是在注解旁边再抄一份，这样路由的
#: 校验、它生成的 OpenAPI 枚举与文档里写的集合不会各自漂移。
AuditResult = Literal["success", "denied", "failed"]
RESULT_OPTIONS: Final[tuple[str, ...]] = get_args(AuditResult)


class AuditLogResponse(BaseModel):
    """一条审计事件。

    `metadata_json` 是故意不出现在响应里的：它是一个自由格式字段，允许的键从未定义过，而对外
    出口不该携带一份没有定义的契约。等键被枚举清楚、可以有针对性地脱敏之后再加。
    """

    id: int
    actor_id: int | None
    action: str
    resource_type: str
    resource_id: str | None
    result: str
    request_id: str | None
    ip_address: str | None
    created_at: str


class AuditLogListResponse(BaseModel):
    """一页审计事件，最新的在前。"""

    items: list[AuditLogResponse]
    page: int
    page_size: int
    total: int


def _event(event: AuditLog) -> AuditLogResponse:
    """序列化一行审计记录，并把存储的客户端地址还原成可读形式。"""
    return AuditLogResponse(
        id=event.id,
        actor_id=event.actor_id,
        action=event.action,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        result=event.result,
        request_id=event.request_id,
        ip_address=format_ip_address(event.ip_address),
        created_at=event.created_at.isoformat(),
    )


@router.get("", response_model=AuditLogListResponse)
def read_audit_logs(
    user: AuditUser,
    session: DbSession,
    actor_id: Annotated[int | None, Query(ge=1)] = None,
    action: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    resource_type: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    resource_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    result: Annotated[
        AuditResult | None,
        Query(description=f"one of: {', '.join(RESULT_OPTIONS)}"),
    ] = None,
    created_after: Annotated[UtcDateTime | None, Query()] = None,
    created_before: Annotated[UtcDateTime | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> AuditLogListResponse:
    """返回一页审计事件。

    `result` 是一个封闭集合（`success` / `denied` / `failed`，即表上 CHECK
    约束允许的取值）。未知取值回 `422` 而不是空页，因为「没有这样的行」和
    「你要的结果类型根本不存在」对读这份轨迹的人是两件不同的事。
    """
    page_result = list_audit_logs(
        session,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        created_after=created_after,
        created_before=created_before,
        page=page,
        page_size=page_size,
    )
    return AuditLogListResponse(
        items=[_event(event) for event in page_result.items],
        page=page_result.page,
        page_size=page_result.page_size,
        total=page_result.total,
    )
