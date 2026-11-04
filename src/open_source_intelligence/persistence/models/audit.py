"""只追加的审计事件模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, LargeBinary, String
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UTCDateTime, uint64_pk, unsigned_bigint, utc_now
from .identity import User

#: `audit_logs.result` 允许的取值，与下面的 `result_valid` 约束一一对应。单独命名的用意是
#: 让审计页的筛选器只提供这张表真正存得下的值：筛选项里出现一个不可能存在的值，会返回空页面，
#: 读起来像是查询坏了，而不是结果为空。
AUDIT_RESULTS: tuple[str, ...] = ("success", "denied", "failed")


class AuditLog(Base):
    """一条安全与运维审计事件。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(
            f"result IN ({', '.join(repr(value) for value in AUDIT_RESULTS)})",
            name="result_valid",
        ),
        Index("ix_audit_logs_actor_created", "actor_id", "created_at"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
        Index("ix_audit_logs_action_created", "action", "created_at"),
    )

    id: Mapped[uint64_pk]
    actor_id: Mapped[int | None] = mapped_column(
        unsigned_bigint(),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mysql_var_binary = mysql.VARBINARY(16)
    ip_address: Mapped[bytes | None] = mapped_column(
        LargeBinary(16).with_variant(mysql_var_binary, "mysql"),
        nullable=True,
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )

    actor: Mapped[User | None] = relationship()
