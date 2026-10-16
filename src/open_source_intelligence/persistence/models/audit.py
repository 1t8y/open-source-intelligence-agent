"""Append-only audit event model."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, LargeBinary, String
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UTCDateTime, uint64_pk, unsigned_bigint, utc_now
from .identity import User

#: The values `audit_logs.result` accepts, mirroring the `result_valid` check below. Named so the
#: audit page's filter offers exactly the values the table can hold: a filter listing a value that
#: cannot exist returns an empty page and reads as a broken query rather than as an empty result.
AUDIT_RESULTS: tuple[str, ...] = ("success", "denied", "failed")


class AuditLog(Base):
    """Security and operational audit event."""

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
