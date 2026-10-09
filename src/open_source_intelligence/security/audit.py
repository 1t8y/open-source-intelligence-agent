"""Audit event creation helpers."""

import ipaddress
from typing import Any

from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.audit import AuditLog


def pack_ip_address(value: str | None) -> bytes | None:
    """Convert an IPv4 or IPv6 address into its compact binary form."""
    if not value:
        return None
    try:
        return ipaddress.ip_address(value).packed
    except ValueError:
        return None


def record_audit_event(
    session: Session,
    *,
    action: str,
    resource_type: str,
    result: str,
    actor_id: int | None = None,
    resource_id: str | None = None,
    request_id: str | None = None,
    ip_address: str | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> AuditLog:
    """Stage one audit event in the caller's transaction."""
    event = AuditLog(
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        request_id=request_id,
        ip_address=pack_ip_address(ip_address),
        metadata_json=metadata_json,
    )
    session.add(event)
    return event
