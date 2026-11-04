"""推送端点：投递允许清单、投递轨迹，以及重试驱动器。

这些端点刻意放在自己的前缀下，而不是 `/api/v1/reports` 下：像
`/reports/push-targets` 这样的字面量路径会被 `/{report_id}` 路由遮蔽，
除非把它声明在前面，而这类路径越加越多，那个坑就会一直留着。唯一一个
挂在报告上的动作 `POST /api/v1/reports/{report_id}/push` 仍与其他报告
动作并列，留在它该在的位置。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from open_source_intelligence.notifications.service import (
    PUSH_BATCH_LIMIT,
    list_pushes,
    list_targets,
    retry_failed_pushes,
)
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.security.authorization import (
    REPORT_WRITE_ROLES,
    readable_reports_filter,
)
from open_source_intelligence.security.dependencies import get_db_session, require_roles
from open_source_intelligence.security.service import get_role_codes

router = APIRouter(prefix="/api/v1/push", tags=["push"])

WriteUser = Annotated[User, Depends(require_roles(*REPORT_WRITE_ROLES))]
DbSession = Annotated[Session, Depends(get_db_session)]

PushChannel = str


class PushTargetResponse(BaseModel):
    """一个在允许清单内的投递目标。"""

    id: int
    name: str
    channel: str
    destination: str
    description: str | None
    enabled: bool
    allow_private: bool
    allow_confidential: bool


class PushTargetListResponse(BaseModel):
    """投递允许清单。"""

    items: list[PushTargetResponse]


class PushRecordResponse(BaseModel):
    """一条投递记录。"""

    id: int
    report_id: int
    version: int
    channel: str
    destination: str
    status: str
    attempt: int
    error_code: str | None
    error_message: str | None
    sent_at: str | None
    created_at: str
    updated_at: str


class PushRecordListResponse(BaseModel):
    """投递记录，最新的在前。"""

    items: list[PushRecordResponse]
    status_counts: dict[str, int]


class PushOutcomeResponse(BaseModel):
    """一次投递尝试的结果。"""

    push_id: int
    report_id: int
    version: int
    channel: str
    destination: str
    status: str
    attempt: int
    error_code: str | None
    error_message: str | None
    delivered: bool
    replayed: bool


class RetryPushResponse(BaseModel):
    """一轮重试扫描的结果。"""

    attempted_count: int
    delivered_count: int
    failed_count: int
    outcomes: list[PushOutcomeResponse]


@router.get("/targets", response_model=PushTargetListResponse)
def get_push_targets(
    user: WriteUser,
    session: DbSession,
    channel: Annotated[PushChannel | None, Query()] = None,
) -> PushTargetListResponse:
    """返回已启用的投递允许清单，可只取某一个渠道。"""
    targets = list_targets(session, channel=channel)
    return PushTargetListResponse(
        items=[
            PushTargetResponse(
                id=target.id,
                name=target.name,
                channel=target.channel,
                destination=target.destination,
                description=target.description,
                enabled=target.enabled,
                allow_private=target.allow_private,
                allow_confidential=target.allow_confidential,
            )
            for target in targets
        ]
    )


@router.get("/records", response_model=PushRecordListResponse)
def get_push_records(
    user: WriteUser,
    session: DbSession,
    report_id: Annotated[int | None, Query(ge=1)] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PushRecordListResponse:
    """返回投递记录，让运维看清发出去了什么、还有哪些等着重试。

    台账按报告的访问控制过滤，而不只是按角色过滤：一条投递记录会带上报告
    标题、版本、目标地址与错误文本，这些元数据已足够泄露一份调用方本来打
    不开的报告。
    """
    rows = list_pushes(
        session,
        report_id=report_id,
        status=status_filter,
        limit=limit,
        access_filter=readable_reports_filter(user, get_role_codes(user)),
    )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return PushRecordListResponse(
        items=[
            PushRecordResponse(
                id=row.id,
                report_id=row.report_id,
                version=row.version,
                channel=row.channel,
                destination=row.destination,
                status=row.status,
                attempt=row.attempt,
                error_code=row.error_code,
                error_message=row.error_message,
                sent_at=row.sent_at.isoformat() if row.sent_at else None,
                created_at=row.created_at.isoformat(),
                updated_at=row.updated_at.isoformat(),
            )
            for row in rows
        ],
        status_counts=counts,
    )


@router.post("/retry", response_model=RetryPushResponse)
def retry_pushes(
    user: WriteUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=PUSH_BATCH_LIMIT)] = PUSH_BATCH_LIMIT,
) -> RetryPushResponse:
    """重试那些还剩重试预算的失败投递。

    这与 Celery beat 任务跑的扫描是同一件事，暴露出来是为了让运维不必等
    下一个周期。一行数据不可用从不会中断整批。扫描范围限定在调用方可读的
    报告内，所以按一下「重试」不会把他看不见的报告又发出去一次。
    """
    outcomes = retry_failed_pushes(
        session,
        limit=limit,
        actor_id=user.id,
        access_filter=readable_reports_filter(user, get_role_codes(user)),
    )
    delivered = [item for item in outcomes if item.delivered]
    return RetryPushResponse(
        attempted_count=len(outcomes),
        delivered_count=len(delivered),
        failed_count=len(outcomes) - len(delivered),
        outcomes=[PushOutcomeResponse(**item.to_dict()) for item in outcomes],
    )
