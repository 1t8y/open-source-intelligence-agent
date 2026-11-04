"""审计事件创建的辅助函数。"""

import ipaddress
from typing import Any

from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.audit import AuditLog


def pack_ip_address(value: str | None) -> bytes | None:
    """把 IPv4 或 IPv6 地址转成紧凑的二进制形式。"""
    if not value:
        return None
    try:
        return ipaddress.ip_address(value).packed
    except ValueError:
        return None


def format_ip_address(value: bytes | None) -> str | None:
    """把打包后的地址转回文本，供对外输出使用。

    它是 `pack_ip_address` 的对应物，放在一起是为了两者始终同步：同一列既存 IPv4
    （4 字节）又存 IPv6（16 字节），而只有这一对函数知道这件事。打包侧会拒绝的值
    不可能出现在这里，因此结果只有两种：`None` 或一个文本地址。
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
    """在调用方的事务中挂起一条审计事件。"""
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
