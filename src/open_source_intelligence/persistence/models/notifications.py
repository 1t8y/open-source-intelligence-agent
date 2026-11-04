"""推送目标与投递记录的持久化模型。

两张表按与发布相关表相同的方式切分职责：`push_targets` 是运维维护的*白名单*，
`report_pushes` 是实际投递内容的只追加流水。正是白名单让「只发往已配置的目的地」成为
可执行的约束；没有它，推送端点会乐于把报告发往任何被告知的地方。

`report_pushes.idempotency_key` 是投递的唯一性句柄：一行对应一个
`(report, version, target)` 组合。因此重复请求同一次投递会命中已有的行，而不是
发两遍；重试是在同一行上推进 `attempt`，而不是新增一行。
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
    """一个被列入白名单的、用于投递已发布报告的目的地。

    `allow_private` 与 `allow_confidential` 是两个收紧开关：只有目标被明确放行时，
    它才会收到 `PRIVATE` 或 `CONFIDENTIAL` 报告。两者默认都是 `False`，因此新增
    的目的地默认是安全的，除非有人主动放宽。
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
    """一个报告版本发往某个目的地的投递尝试流水。

    `channel` 与 `destination` 是刻意冗余的：即使运维后来改了指往的地方、或删掉了当初
    投递所用的白名单条目，审计行也必须仍然可读。只要白名单条目还在，`target_id` 外键
    仍把这行系回该条目。
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
    #: 发送时刻的目标名称。冗余的理由与 `channel`、`destination` 相同：流水需要写明
    #: 目的地，而在这一列出现之前，它是靠连接白名单来解析这个名字的 —— 而白名单只有
    #: `OPS_CONFIG_ROLES` 能读，于是编辑对每一行看到的都是「（目标 #29 已不存在）」，
    #: 尽管那个目标还活着、也还启用着。一条会因为*读者*的角色而变成墓碑的审计行，已经
    #: 不是审计行。
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
        """返回某个报告版本与目标组合的稳定幂等键。"""
        return f"report:{report_id}:v{version}:target:{target_id}"
