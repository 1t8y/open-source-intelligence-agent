"""Audit log endpoint: the read-only view over the append-only trail.

`docs/api-contract.md` §8 fixes the shape. The endpoint is read-only in the strongest available
sense — there is no update or delete route to protect, so no misconfiguration can open one.
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

#: The three outcomes `audit_logs.result` permits. The list is derived from the annotation
#: rather than repeated next to it, so the route's validation, its OpenAPI enum and the
#: documented set cannot drift apart.
AuditResult = Literal["success", "denied", "failed"]
RESULT_OPTIONS: Final[tuple[str, ...]] = get_args(AuditResult)


class AuditLogResponse(BaseModel):
    """One audit event.

    `metadata_json` is absent on purpose: it is a free-form field whose allowed keys were never
    defined, and an egress path should not ship an undefined contract. Add it once the keys are
    enumerated and can be redacted deliberately.
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
    """One page of audit events, newest first."""

    items: list[AuditLogResponse]
    page: int
    page_size: int
    total: int


def _event(event: AuditLog) -> AuditLogResponse:
    """Serialize one audit row, unpacking the stored client address."""
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
    """Return one page of audit events.

    `result` is a closed set (`success` / `denied` / `failed`, the values the table's CHECK
    constraint permits). An unknown value is a `422` rather than an empty page, because "there
    are no rows" and "you asked for a result type that cannot exist" are different findings to
    whoever is reading the trail.
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
