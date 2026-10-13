"""Evaluation dataset, case, run, and result persistence models.

The four tables answer one question each, and the split matters:

* `eval_datasets` is the *identity* of a frozen dataset. `(name, version)` is unique and a
  dataset is never edited in place - changing a case means publishing a new version - so a
  result row can always be traced back to the exact sample set that produced it.
* `eval_cases` is one sample: its input snapshot, the human reference, and the rubric. A case
  owns no results; the same case is measured by many runs.
* `eval_runs` is one execution of one dataset against one *target variant*
  (`baseline_single_prompt`, `agent_full`, and the three ablations). `summary_json` is where
  the unified comparison report lives, so an operator reads one row per variant instead of
  re-aggregating hundreds of result rows.
* `eval_results` is the per-case measurement. `(eval_run_id, eval_case_id)` is unique, so a
  re-run of a case overwrites its own measurement instead of appending a duplicate.

The three metric columns that `docs/data-model.md` §7 fixes (`citation_coverage`,
`factual_consistency`, `task_completion`) are promoted to real columns because they are the
gated release criteria; every other metric stays in `details_json`.
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

#: The five variants `docs/evaluation.md` §1 requires, byte-identical to that table.
EVAL_TARGET_TYPES = (
    "baseline_single_prompt",
    "agent_full",
    "agent_no_verifier",
    "agent_no_tools",
    "agent_no_history",
)

EVAL_RUN_STATUSES = ("pending", "running", "succeeded", "failed")


class EvalDataset(Base, TimestampMixin):
    """One immutable version of an evaluation dataset.

    `(name, version)` is the identity: raising the version is the only way to change a
    dataset, which keeps every historical result reproducible.
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
    """One sample inside a dataset version.

    `input_json` is the frozen snapshot the target reads; `reference_json` and `rubric_json`
    are what the deterministic scorers compare against. `enabled` lets a case be parked
    without deleting it, and a disabled case is skipped by the runner rather than scored zero.
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
    """One dataset-against-target execution, and the holder of the unified report.

    `finished_at >= started_at` is enforced the same way `pipeline_runs` enforces it, because
    a run whose clock runs backwards is a broken record rather than a slow one.
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
    """One case's measured outcome inside one run.

    Rates are stored as `Numeric(5, 4)` because every one of them is a `0.0`-`1.0` ratio; the
    extra metrics that have no gated threshold (`source_accuracy`, `tool_success_rate`,
    `prompt_injection_block_rate`) live in `details_json` so the schema stays tied to the
    release criteria instead of chasing every new score.
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
