"""Push endpoints: the delivery allowlist, the delivery trail, and the retry driver.

These live under their own prefix rather than under `/api/v1/reports` on purpose: a literal
path such as `/reports/push-targets` would be shadowed by the `/{report_id}` route unless it
were declared first, and adding more of them would keep that trap alive. The single
report-scoped action, `POST /api/v1/reports/{report_id}/push`, stays with the other report
actions where it belongs.
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
from open_source_intelligence.security.authorization import REPORT_WRITE_ROLES
from open_source_intelligence.security.dependencies import get_db_session, require_roles

router = APIRouter(prefix="/api/v1/push", tags=["push"])

WriteUser = Annotated[User, Depends(require_roles(*REPORT_WRITE_ROLES))]
DbSession = Annotated[Session, Depends(get_db_session)]

PushChannel = str


class PushTargetResponse(BaseModel):
    """One allowlisted delivery destination."""

    id: int
    name: str
    channel: str
    destination: str
    description: str | None
    enabled: bool
    allow_private: bool
    allow_confidential: bool


class PushTargetListResponse(BaseModel):
    """The delivery allowlist."""

    items: list[PushTargetResponse]


class PushRecordResponse(BaseModel):
    """One delivery record."""

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
    """Delivery records, newest first."""

    items: list[PushRecordResponse]
    status_counts: dict[str, int]


class PushOutcomeResponse(BaseModel):
    """Result of one delivery attempt."""

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
    """Result of one retry sweep."""

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
    """Return the enabled delivery allowlist, optionally for one channel."""
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
    """Return delivery records so an operator can see what went out and what still needs a retry."""
    rows = list_pushes(session, report_id=report_id, status=status_filter, limit=limit)
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
    """Retry the failed deliveries that still have retry budget left.

    This is the same sweep the Celery beat task runs, exposed so an operator does not have to
    wait for the next tick. One unusable row never stops the batch.
    """
    outcomes = retry_failed_pushes(session, limit=limit, actor_id=user.id)
    delivered = [item for item in outcomes if item.delivered]
    return RetryPushResponse(
        attempted_count=len(outcomes),
        delivered_count=len(delivered),
        failed_count=len(outcomes) - len(delivered),
        outcomes=[PushOutcomeResponse(**item.to_dict()) for item in outcomes],
    )
