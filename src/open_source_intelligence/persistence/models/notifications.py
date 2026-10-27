"""Push target and delivery-record persistence models.

Two tables split the concern in the same way the publishing tables do: `push_targets` is the
*allowlist* an operator maintains, and `report_pushes` is the append-only trail of what was
actually delivered. The allowlist is what makes "only configured destinations" enforceable;
without it a push endpoint would happily send a report anywhere it was told.

`report_pushes.idempotency_key` is the uniqueness handle for delivery: one row per
(report, version, target). Re-requesting a delivery therefore finds the existing row instead of
sending twice, and a retry advances `attempt` on that same row rather than adding a new one.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import (
    Base,
    TimestampMixin,
    UTCDateTime,
    uint64_pk,
    unsigned_bigint,
)

PUSH_CHANNELS = ("email", "webhook")
PUSH_STATUSES = ("pending", "sent", "failed")


class PushTarget(Base, TimestampMixin):
    """One allowlisted delivery destination for published reports.

    `allow_private` and `allow_confidential` are the two narrowing switches: a target only
    receives a `PRIVATE` or `CONFIDENTIAL` report when it has been explicitly cleared for it.
    Both default to `False`, so a newly added destination is safe until someone widens it.
    """

    __tablename__ = "push_targets"
    __table_args__ = (
        CheckConstraint("channel IN ('email', 'webhook')", name="channel_valid"),
        UniqueConstraint("name"),
        Index("ix_push_targets_channel_enabled", "channel", "enabled"),
    )

    id: Mapped[uint64_pk]
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    destination: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_confidential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ReportPush(Base, TimestampMixin):
    """One delivery attempt trail for a report version sent to a target.

    `channel` and `destination` are denormalized on purpose: an audit row has to stay readable
    even after an operator retargets or removes the allowlist entry it was sent to. The
    `target_id` foreign key still ties the row back to the allowlist entry while it exists.
    """

    __tablename__ = "report_pushes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed')",
            name="status_valid",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        UniqueConstraint("idempotency_key"),
        Index("ix_report_pushes_report_version", "report_id", "version"),
        Index("ix_report_pushes_status_updated", "status", "updated_at"),
    )

    id: Mapped[uint64_pk]
    report_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("reports.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("push_targets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    destination: Mapped[str] = mapped_column(String(512), nullable=False)
    #: The target's name at send time. Denormalized for the same reason as `channel` and
    #: `destination`: the ledger names the destination, and until this column existed it resolved
    #: that name by joining the allowlist — which is `OPS_CONFIG_ROLES`-only, so an editor saw
    #: "（目标 #29 已不存在）" for every row while the target was alive and enabled. An audit row
    #: that turns into a tombstone because of the *reader's* role is not an audit row.
    target_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    message_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    response_excerpt: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pipeline_run_id: Mapped[int | None] = mapped_column(
        unsigned_bigint(),
        ForeignKey("pipeline_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    @staticmethod
    def key_for(*, report_id: int, version: int, target_id: int) -> str:
        """Return the stable idempotency key for one report version and target pair."""
        return f"report:{report_id}:v{version}:target:{target_id}"
