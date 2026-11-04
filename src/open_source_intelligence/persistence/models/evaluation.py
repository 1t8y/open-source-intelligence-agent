"""评测数据集、用例、运行与结果的持久化模型。

这四张表各回答一个问题，这种拆分本身就有意义：

* `eval_datasets` 是一个已冻结数据集的*身份*。`(name, version)` 唯一，数据集永远
  不做原地修改 —— 改用例就意味着发布新版本 —— 因此任何一行结果都能回溯到产生它的
  那批确切样本。
* `eval_cases` 是一个样本：它的输入快照、人工参考答案与评分细则。用例不拥有结果；
  同一个用例会被多次运行测量。
* `eval_runs` 是拿一个数据集对一个*目标变体*的一次执行
  （`baseline_single_prompt`、`agent_full`，以及三个消融变体）。`summary_json`
  存放统一对比报告，运维因此只需读每个变体一行，而不必重新聚合上百行结果。
* `eval_results` 是按用例的测量结果。`(eval_run_id, eval_case_id)` 唯一，因此重跑
  某个用例会覆盖它自己的测量值，而不是追加一条重复记录。

`docs/data-model.md` §7 定下的三个指标列（`citation_coverage`、
`factual_consistency`、`task_completion`）被提升为真实列，因为它们正是发布门禁的
判据；其余指标一律留在 `details_json` 中。
"""

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

#: `docs/evaluation.md` §1 要求的五个变体，与那张表逐字节一致。
EVAL_TARGET_TYPES = (
    "baseline_single_prompt",
    "agent_full",
    "agent_no_verifier",
    "agent_no_tools",
    "agent_no_history",
)

EVAL_RUN_STATUSES = ("pending", "running", "succeeded", "failed")


class EvalDataset(Base, TimestampMixin):
    """评测数据集的一个不可变版本。

    `(name, version)` 就是身份：提升版本号是修改数据集的唯一方式，这样每一条历史结果
    都保持可复现。
    """

    __tablename__ = "eval_datasets"
    __table_args__ = (
        UniqueConstraint("name", "version"),
        Index("ix_eval_datasets_name_created", "name", "created_at"),
    )

    id: Mapped[uint64_pk]
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        unsigned_bigint(),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class EvalCase(Base, TimestampMixin):
    """数据集某个版本内的一个样本。

    `input_json` 是被测目标读取的冻结快照；`reference_json` 与 `rubric_json` 是确定性
    评分器用来比对的依据。`enabled` 允许把一个用例搁置起来而不删除它，被禁用的用例会
    被运行器跳过，而不是记零分。
    """

    __tablename__ = "eval_cases"
    __table_args__ = (
        UniqueConstraint("dataset_id", "case_key"),
        Index("ix_eval_cases_dataset_enabled", "dataset_id", "enabled"),
    )

    id: Mapped[uint64_pk]
    dataset_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("eval_datasets.id", ondelete="CASCADE"),
        nullable=False,
    )
    case_key: Mapped[str] = mapped_column(String(64), nullable=False)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reference_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    rubric_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class EvalRun(Base, TimestampMixin):
    """一次「数据集对目标变体」的执行，同时也是统一报告的存放处。

    `finished_at >= started_at` 的约束方式与 `pipeline_runs` 完全一致：时钟倒着走的
    运行是一条坏记录，而不是一条慢记录。
    """

    __tablename__ = "eval_runs"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('baseline_single_prompt', 'agent_full', 'agent_no_verifier', "
            "'agent_no_tools', 'agent_no_history')",
            name="target_type_valid",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="status_valid",
        ),
        CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name="finished_after_started",
        ),
        Index("ix_eval_runs_dataset_target", "dataset_id", "target_type"),
        Index("ix_eval_runs_status_created", "status", "created_at"),
    )

    id: Mapped[uint64_pk]
    dataset_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("eval_datasets.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_config_id: Mapped[int | None] = mapped_column(
        unsigned_bigint(),
        ForeignKey("agent_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)


class EvalResult(Base, TimestampMixin):
    """某一个用例在某一次运行内的测量结果。

    比率一律存为 `Numeric(5, 4)`，因为每一个都是 `0.0`-`1.0` 的比值；没有发布门槛的
    额外指标（`source_accuracy`、`tool_success_rate`、
    `prompt_injection_block_rate`）放在 `details_json` 里，好让表结构紧扣发布判据，
    而不是每来一个新分数就改一次结构。
    """

    __tablename__ = "eval_results"
    __table_args__ = (
        CheckConstraint(
            "human_score IS NULL OR (human_score >= 1 AND human_score <= 5)",
            name="human_score_range",
        ),
        CheckConstraint("cost >= 0", name="cost_nonnegative"),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="latency_nonnegative",
        ),
        UniqueConstraint("eval_run_id", "eval_case_id"),
        Index("ix_eval_results_run_status", "eval_run_id", "status"),
    )

    id: Mapped[uint64_pk]
    eval_run_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("eval_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    eval_case_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("eval_cases.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="succeeded")
    citation_coverage: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    factual_consistency: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    task_completion: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    cost: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=Decimal("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    human_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
