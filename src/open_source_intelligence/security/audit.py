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


def format_ip_address(value: bytes | None) -> str | None:
    """Convert a packed address back to text for egress.

    The counterpart of `pack_ip_address`, kept next to it so the two stay in step: one column
    holds both IPv4 (4 bytes) and IPv6 (16 bytes), and only the pair of functions knows that.
    A value the packing side would have rejected cannot appear here, so `None` and a textual
    address are the only two outcomes.
    """
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
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
