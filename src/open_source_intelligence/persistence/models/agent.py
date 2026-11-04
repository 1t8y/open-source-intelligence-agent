"""Agent 配置、运行、步骤与工具调用的持久化模型。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, UTCDateTime, uint64_pk, unsigned_bigint, utc_now

AGENT_RUN_STATUSES = ("pending", "running", "needs_review", "completed", "failed")
AGENT_STEP_TYPES = ("plan", "tool", "observe", "verify", "write")
TOOL_CALL_STATUSES = ("success", "failed", "denied")
#: 人工读完一次 `needs_review` 运行后给出的结论。
#:
#: 刻意不叫「approve/reject」：运行是一个已经发生的事实，而不是一份提案。这里没有东西可批准
#: —— 审核者要回答的问题是「这份解释是否已经够好、可以不再看下去，还是需要再试一次」。因此
#: 结论描述的是*下一步动作*，它不改变运行自身的状态：无论给出哪种结论，这次运行依然没有报告。
AGENT_RUN_REVIEW_VERDICTS = ("acknowledged", "needs_rerun")


class AgentConfig(Base):
    """带版本的 Agent 模型、Prompt 与工具预算配置。"""

    __tablename__ = "agent_configs"
    __table_args__ = (
        CheckConstraint("max_loops > 0", name="max_loops_positive"),
        CheckConstraint("max_model_calls > 0", name="max_model_calls_positive"),
        CheckConstraint("max_tool_calls > 0", name="max_tool_calls_positive"),
        CheckConstraint("max_tokens > 0", name="max_tokens_positive"),
        CheckConstraint("max_cost >= 0", name="max_cost_nonnegative"),
        UniqueConstraint("name", "version"),
    )

    id: Mapped[uint64_pk]
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    max_loops: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    max_model_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    #: 与 `Settings.agent_max_tool_calls` 保持一致。两者都刻意高于证据下限（默认五个候选时
    #: `2 x candidates + 2` = 12）：预算恰好等于下限，就等于不给任何一次白跑的调用留余地。
    #: 参见 `agent/runtime.py:evidence_tool_floor`。
    max_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=12000)
    max_cost: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=Decimal("0"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )


class AgentRun(Base):
    """一次研究 Agent 执行，绑定到某个流水线运行与主题。"""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'needs_review', 'completed', 'failed')",
            name="status_valid",
        ),
        CheckConstraint("loop_count >= 0", name="loop_count_nonnegative"),
        CheckConstraint("tool_call_count >= 0", name="tool_call_count_nonnegative"),
        CheckConstraint("model_call_count >= 0", name="model_call_count_nonnegative"),
        CheckConstraint("prompt_tokens >= 0", name="prompt_tokens_nonnegative"),
        CheckConstraint("completion_tokens >= 0", name="completion_tokens_nonnegative"),
        CheckConstraint("estimated_cost >= 0", name="estimated_cost_nonnegative"),
        CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name="time_order_valid",
        ),
        Index("ix_agent_runs_topic_status", "topic_id", "status"),
        Index("ix_agent_runs_started_at", "started_at"),
        Index("ix_agent_runs_config", "agent_config_id"),
    )

    id: Mapped[uint64_pk]
    pipeline_run_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("pipeline_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    topic_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("topics.id", ondelete="RESTRICT"),
        nullable=False,
    )
    agent_config_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("agent_configs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    window_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    window_end: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    loop_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 6),
        nullable=False,
        default=Decimal("0"),
    )
    publish_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision_reason_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AgentStep(Base):
    """Agent 运行内一个按序编号的步骤。"""

    __tablename__ = "agent_steps"
    __table_args__ = (
        CheckConstraint("step_index >= 1", name="step_index_positive"),
        CheckConstraint(
            "step_type IN ('plan', 'tool', 'observe', 'verify', 'write')",
            name="step_type_valid",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="latency_nonnegative",
        ),
        UniqueConstraint("agent_run_id", "step_index"),
        Index("ix_agent_steps_run", "agent_run_id"),
    )

    id: Mapped[uint64_pk]
    agent_run_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )


class ToolCall(Base):
    """一次受白名单约束的 Agent 工具调用记录。"""

    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint("status IN ('success', 'failed', 'denied')", name="status_valid"),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="latency_nonnegative",
        ),
        Index("ix_tool_calls_agent_run", "agent_run_id"),
        Index("ix_tool_calls_tool_status", "tool_name", "status"),
    )

    id: Mapped[uint64_pk]
    agent_run_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_step_id: Mapped[int | None] = mapped_column(
        unsigned_bigint(),
        ForeignKey("agent_steps.id", ondelete="SET NULL"),
        nullable=True,
    )
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )


class AgentRunReview(Base):
    """人工对一次「没产出报告就停下」的运行所下的结论。

    `needs_review` 运行是终态：下游没有任何环节能批准它，因为它没有产出可批准的草稿。真正缺
    的是一个存放*阅读结论*的地方 —— 运维打开轨迹，判断证据缺口是否已经理解清楚、还是需要再
    试一次，而这个判断无处可落，同一批数据行于是永远保持原样、也永远无人解释。

    数据行与 `report_reviews` 一样只追加：重读一次运行是新增一行，而不是改写上一行，因此
    「谁在什么时候想了什么」在改变主意之后依然留得下来。
    """

    __tablename__ = "agent_run_reviews"
    __table_args__ = (
        CheckConstraint(
            "verdict IN ('acknowledged', 'needs_rerun')",
            name="verdict_valid",
        ),
        Index("ix_agent_run_reviews_run_created", "agent_run_id", "created_at"),
    )

    id: Mapped[uint64_pk]
    agent_run_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    reviewer_id: Mapped[int | None] = mapped_column(
        unsigned_bigint(),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )
