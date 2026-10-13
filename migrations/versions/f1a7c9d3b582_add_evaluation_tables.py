"""add evaluation datasets, cases, runs, and results

Revision ID: f1a7c9d3b582
Revises: d4f8b2c6a1e9
Create Date: 2026-10-13 21:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "f1a7c9d3b582"
down_revision: str | None = "d4f8b2c6a1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def utc_datetime() -> sa.DateTime:
    """Return the project MySQL UTC DATETIME type."""
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def uint64() -> sa.BigInteger:
    """Return the project unsigned BIGINT type."""
    return sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def upgrade() -> None:
    """Create the four evaluation tables.

    `eval_datasets` is versioned and immutable, `eval_cases` holds the frozen samples,
    `eval_runs` records one dataset-against-target execution, and `eval_results` holds the
    per-case measurement. Deleting a dataset therefore removes its cases, its runs, and
    their results in one cascade, which is what keeps the evaluation tables from leaking
    orphans during a re-import.
    """
    op.create_table(
        "eval_datasets",
        sa.Column("id", uint64(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("created_by", uint64(), nullable=True),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.Column("updated_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_eval_datasets_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_datasets")),
        sa.UniqueConstraint("name", "version", name=op.f("uq_eval_datasets_name")),
    )
    op.create_index(
        "ix_eval_datasets_name_created",
        "eval_datasets",
        ["name", "created_at"],
        unique=False,
    )

    op.create_table(
        "eval_cases",
        sa.Column("id", uint64(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", uint64(), nullable=False),
        sa.Column("case_key", sa.String(length=64), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("reference_json", sa.JSON(), nullable=True),
        sa.Column("rubric_json", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.Column("updated_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["eval_datasets.id"],
            name=op.f("fk_eval_cases_dataset_id_eval_datasets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_cases")),
        sa.UniqueConstraint("dataset_id", "case_key", name=op.f("uq_eval_cases_dataset_id")),
    )
    op.create_index(
        "ix_eval_cases_dataset_enabled",
        "eval_cases",
        ["dataset_id", "enabled"],
        unique=False,
    )

    op.create_table(
        "eval_runs",
        sa.Column("id", uint64(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", uint64(), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("agent_config_id", uint64(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", utc_datetime(), nullable=True),
        sa.Column("finished_at", utc_datetime(), nullable=True),
        sa.Column("summary_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.Column("updated_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_config_id"],
            ["agent_configs.id"],
            name=op.f("fk_eval_runs_agent_config_id_agent_configs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["eval_datasets.id"],
            name=op.f("fk_eval_runs_dataset_id_eval_datasets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_runs")),
    )
    op.execute(
        sa.text(
            "ALTER TABLE `eval_runs` "
            "ADD CONSTRAINT `ck_eval_runs_target_type_valid` "
            "CHECK (`target_type` IN ('baseline_single_prompt', 'agent_full', "
            "'agent_no_verifier', 'agent_no_tools', 'agent_no_history'))"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE `eval_runs` "
            "ADD CONSTRAINT `ck_eval_runs_status_valid` "
            "CHECK (`status` IN ('pending', 'running', 'succeeded', 'failed'))"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE `eval_runs` "
            "ADD CONSTRAINT `ck_eval_runs_finished_after_started` "
            "CHECK (`finished_at` IS NULL OR `started_at` IS NULL OR `finished_at` >= `started_at`)"
        )
    )
    op.create_index(
        "ix_eval_runs_dataset_target",
        "eval_runs",
        ["dataset_id", "target_type"],
        unique=False,
    )
    op.create_index(
        "ix_eval_runs_status_created",
        "eval_runs",
        ["status", "created_at"],
        unique=False,
    )

    op.create_table(
        "eval_results",
        sa.Column("id", uint64(), autoincrement=True, nullable=False),
        sa.Column("eval_run_id", uint64(), nullable=False),
        sa.Column("eval_case_id", uint64(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("citation_coverage", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("factual_consistency", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("task_completion", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("cost", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("human_score", sa.Integer(), nullable=True),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.Column("updated_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["eval_case_id"],
            ["eval_cases.id"],
            name=op.f("fk_eval_results_eval_case_id_eval_cases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["eval_run_id"],
            ["eval_runs.id"],
            name=op.f("fk_eval_results_eval_run_id_eval_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_results")),
        sa.UniqueConstraint(
            "eval_run_id",
            "eval_case_id",
            name=op.f("uq_eval_results_eval_run_id"),
        ),
    )
    op.execute(
        sa.text(
            "ALTER TABLE `eval_results` "
            "ADD CONSTRAINT `ck_eval_results_human_score_range` "
            "CHECK (`human_score` IS NULL OR (`human_score` >= 1 AND `human_score` <= 5))"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE `eval_results` "
            "ADD CONSTRAINT `ck_eval_results_cost_nonnegative` CHECK (`cost` >= 0)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE `eval_results` "
            "ADD CONSTRAINT `ck_eval_results_latency_nonnegative` "
            "CHECK (`latency_ms` IS NULL OR `latency_ms` >= 0)"
        )
    )
    op.create_index(
        "ix_eval_results_run_status",
        "eval_results",
        ["eval_run_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the evaluation tables in reverse dependency order."""
    op.drop_index("ix_eval_results_run_status", table_name="eval_results")
    op.execute(
        sa.text("ALTER TABLE `eval_results` DROP CHECK `ck_eval_results_latency_nonnegative`")
    )
    op.execute(sa.text("ALTER TABLE `eval_results` DROP CHECK `ck_eval_results_cost_nonnegative`"))
    op.execute(sa.text("ALTER TABLE `eval_results` DROP CHECK `ck_eval_results_human_score_range`"))
    op.drop_table("eval_results")

    op.drop_index("ix_eval_runs_status_created", table_name="eval_runs")
    op.drop_index("ix_eval_runs_dataset_target", table_name="eval_runs")
    op.execute(sa.text("ALTER TABLE `eval_runs` DROP CHECK `ck_eval_runs_finished_after_started`"))
    op.execute(sa.text("ALTER TABLE `eval_runs` DROP CHECK `ck_eval_runs_status_valid`"))
    op.execute(sa.text("ALTER TABLE `eval_runs` DROP CHECK `ck_eval_runs_target_type_valid`"))
    op.drop_table("eval_runs")

    op.drop_index("ix_eval_cases_dataset_enabled", table_name="eval_cases")
    op.drop_table("eval_cases")

    op.drop_index("ix_eval_datasets_name_created", table_name="eval_datasets")
    op.drop_table("eval_datasets")
