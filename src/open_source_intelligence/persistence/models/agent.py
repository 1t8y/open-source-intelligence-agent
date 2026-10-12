"""Agent configuration, run, step, and tool-call persistence models."""

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


class AgentConfig(Base):
    """Versioned Agent model, prompt, and tool-budget configuration."""

    __tablename__ = "agent_configs"
    __table_args__ = (
        CheckConstraint("max_loops > 0", name="max_loops_positive"),
        CheckConstraint("max_model_calls > 0", name="max_model_calls_positive"),
        CheckConstraint("max_tool_calls > 0", name="max_tool_calls_positive"),
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
    max_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    max_cost: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=Decimal("0"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )


class AgentRun(Base):
    """One research Agent execution tied to a pipeline run and topic."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'needs_review', 'completed', 'failed')",
            name="status_valid",
        ),
        CheckConstraint("loop_count >= 0", name="loop_count_nonnegative"),
        CheckConstraint("tool_call_count >= 0", name="tool_call_count_nonnegative"),
        CheckConstraint("model_call_count >= 0", name="model_call_count_nonnegative"),
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
    """One ordered step inside an Agent run."""

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
    """Recorded invocation of one whitelisted Agent tool."""

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
