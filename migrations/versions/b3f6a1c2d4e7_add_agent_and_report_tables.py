"""add agent and report tables

Revision ID: b3f6a1c2d4e7
Revises: 9c1f7b2d4e83
Create Date: 2026-10-12 17:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "b3f6a1c2d4e7"
down_revision: str | None = "9c1f7b2d4e83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UINT = sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")

CHECKS = (
    ("agent_configs", "ck_agent_configs_max_loops_positive", "`max_loops` > 0"),
    ("agent_configs", "ck_agent_configs_max_model_calls_positive", "`max_model_calls` > 0"),
    ("agent_configs", "ck_agent_configs_max_tool_calls_positive", "`max_tool_calls` > 0"),
    ("agent_configs", "ck_agent_configs_max_cost_nonnegative", "`max_cost` >= 0"),
    (
        "agent_runs",
        "ck_agent_runs_status_valid",
        "`status` IN ('pending', 'running', 'needs_review', 'completed', 'failed')",
    ),
    ("agent_runs", "ck_agent_runs_loop_count_nonnegative", "`loop_count` >= 0"),
    ("agent_runs", "ck_agent_runs_tool_call_count_nonnegative", "`tool_call_count` >= 0"),
    ("agent_runs", "ck_agent_runs_model_call_count_nonnegative", "`model_call_count` >= 0"),
    ("agent_runs", "ck_agent_runs_estimated_cost_nonnegative", "`estimated_cost` >= 0"),
    (
        "agent_runs",
        "ck_agent_runs_time_order_valid",
        "`finished_at` IS NULL OR `started_at` IS NULL OR `finished_at` >= `started_at`",
    ),
    ("agent_steps", "ck_agent_steps_step_index_positive", "`step_index` >= 1"),
    (
        "agent_steps",
        "ck_agent_steps_step_type_valid",
        "`step_type` IN ('plan', 'tool', 'observe', 'verify', 'write')",
    ),
    (
        "agent_steps",
        "ck_agent_steps_latency_nonnegative",
        "`latency_ms` IS NULL OR `latency_ms` >= 0",
    ),
    (
        "tool_calls",
        "ck_tool_calls_status_valid",
        "`status` IN ('success', 'failed', 'denied')",
    ),
    (
        "tool_calls",
        "ck_tool_calls_latency_nonnegative",
        "`latency_ms` IS NULL OR `latency_ms` >= 0",
    ),
    (
        "reports",
        "ck_reports_status_valid",
        "`status` IN ('draft', 'pending_review', 'approved', 'scheduled', "
        "'published', 'failed', 'changes_requested')",
    ),
    ("reports", "ck_reports_visibility_valid", "`visibility` IN ('PRIVATE', 'TEAM', 'INTERNAL')"),
    (
        "reports",
        "ck_reports_sensitivity_valid",
        "`sensitivity` IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL')",
    ),
    (
        "reports",
        "ck_reports_publish_mode_valid",
        "`publish_mode` IN ('AUTO', 'AUTO_WITH_DELAY', 'MANUAL')",
    ),
    ("reports", "ck_reports_current_version_positive", "`current_version` >= 1"),
    (
        "report_sources",
        "ck_report_sources_source_type_valid",
        "`source_type` IN ('metric', 'readme', 'prior_report')",
    ),
    (
        "report_drafts",
        "ck_report_drafts_status_valid",
        "`status` IN ('draft', 'superseded', 'promoted')",
    ),
    ("report_sections", "ck_report_sections_version_positive", "`version` >= 1"),
    (
        "repository_contents",
        "ck_repository_contents_content_type_valid",
        "`content_type` IN ('readme')",
    ),
)


def utc_datetime() -> sa.DateTime:
    """Return the MySQL UTC DATETIME type used across the schema."""
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def medium_text() -> sa.Text:
    """Return a TEXT column that maps to MEDIUMTEXT on MySQL."""
    return sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql")


def upgrade() -> None:
    """Create the Agent run, tool call, report, and content cache tables."""
    op.create_table(
        "agent_configs",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("tool_policy_json", sa.JSON(), nullable=False),
        sa.Column("max_loops", sa.Integer(), nullable=False),
        sa.Column("max_model_calls", sa.Integer(), nullable=False),
        sa.Column("max_tool_calls", sa.Integer(), nullable=False),
        sa.Column("max_cost", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_configs")),
        sa.UniqueConstraint("name", "version", name=op.f("uq_agent_configs_name")),
    )
    op.create_table(
        "agent_runs",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("pipeline_run_id", UINT, nullable=False),
        sa.Column("topic_id", UINT, nullable=False),
        sa.Column("agent_config_id", UINT, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("window_start", utc_datetime(), nullable=False),
        sa.Column("window_end", utc_datetime(), nullable=False),
        sa.Column("loop_count", sa.Integer(), nullable=False),
        sa.Column("tool_call_count", sa.Integer(), nullable=False),
        sa.Column("model_call_count", sa.Integer(), nullable=False),
        sa.Column("estimated_cost", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("publish_mode", sa.String(length=32), nullable=True),
        sa.Column("decision_reason_json", sa.JSON(), nullable=True),
        sa.Column("started_at", utc_datetime(), nullable=True),
        sa.Column("finished_at", utc_datetime(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["agent_config_id"],
            ["agent_configs.id"],
            name=op.f("fk_agent_runs_agent_config_id_agent_configs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["pipeline_runs.id"],
            name=op.f("fk_agent_runs_pipeline_run_id_pipeline_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topics.id"],
            name=op.f("fk_agent_runs_topic_id_topics"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
    )
    op.create_index("ix_agent_runs_config", "agent_runs", ["agent_config_id"], unique=False)
    op.create_index("ix_agent_runs_started_at", "agent_runs", ["started_at"], unique=False)
    op.create_index(
        "ix_agent_runs_topic_status", "agent_runs", ["topic_id", "status"], unique=False
    )
    op.create_table(
        "agent_steps",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("agent_run_id", UINT, nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("input_digest", sa.String(length=64), nullable=True),
        sa.Column("output_digest", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_run_id"],
            ["agent_runs.id"],
            name=op.f("fk_agent_steps_agent_run_id_agent_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_steps")),
        sa.UniqueConstraint("agent_run_id", "step_index", name=op.f("uq_agent_steps_agent_run_id")),
    )
    op.create_index("ix_agent_steps_run", "agent_steps", ["agent_run_id"], unique=False)
    op.create_table(
        "tool_calls",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("agent_run_id", UINT, nullable=False),
        sa.Column("agent_step_id", UINT, nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_run_id"],
            ["agent_runs.id"],
            name=op.f("fk_tool_calls_agent_run_id_agent_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_step_id"],
            ["agent_steps.id"],
            name=op.f("fk_tool_calls_agent_step_id_agent_steps"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
    )
    op.create_index("ix_tool_calls_agent_run", "tool_calls", ["agent_run_id"], unique=False)
    op.create_index(
        "ix_tool_calls_tool_status", "tool_calls", ["tool_name", "status"], unique=False
    )
    op.create_table(
        "reports",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("agent_run_id", UINT, nullable=False),
        sa.Column("topic_id", UINT, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("content_markdown", medium_text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("sensitivity", sa.String(length=16), nullable=False),
        sa.Column("publish_mode", sa.String(length=32), nullable=False),
        sa.Column("publish_at", utc_datetime(), nullable=True),
        sa.Column("generated_at", utc_datetime(), nullable=False),
        sa.Column("published_at", utc_datetime(), nullable=True),
        sa.Column("approved_by", UINT, nullable=True),
        sa.ForeignKeyConstraint(
            ["agent_run_id"],
            ["agent_runs.id"],
            name=op.f("fk_reports_agent_run_id_agent_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topics.id"],
            name=op.f("fk_reports_topic_id_topics"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reports")),
    )
    op.create_index("ix_reports_publish_at", "reports", ["publish_at"], unique=False)
    op.create_index("ix_reports_topic_status", "reports", ["topic_id", "status"], unique=False)
    op.create_index(
        "ix_reports_visibility_sensitivity",
        "reports",
        ["visibility", "sensitivity"],
        unique=False,
    )
    op.create_table(
        "report_sources",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("report_id", UINT, nullable=False),
        sa.Column("repository_id", UINT, nullable=True),
        sa.Column("section_key", sa.String(length=64), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String(length=512), nullable=True),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("metric_snapshot_id", UINT, nullable=True),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["reports.id"],
            name=op.f("fk_report_sources_report_id_reports"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_sources")),
    )
    op.create_index(
        "ix_report_sources_report_section",
        "report_sources",
        ["report_id", "section_key"],
        unique=False,
    )
    op.create_index(
        "ix_report_sources_repository", "report_sources", ["repository_id"], unique=False
    )
    op.create_table(
        "report_drafts",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("agent_run_id", UINT, nullable=False),
        sa.Column("topic_id", UINT, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.Column("updated_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_run_id"],
            ["agent_runs.id"],
            name=op.f("fk_report_drafts_agent_run_id_agent_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topics.id"],
            name=op.f("fk_report_drafts_topic_id_topics"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_drafts")),
        sa.UniqueConstraint("agent_run_id", name=op.f("uq_report_drafts_agent_run_id")),
    )
    op.create_table(
        "report_sections",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("report_draft_id", UINT, nullable=False),
        sa.Column("section_key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_markdown", medium_text(), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("citations_json", sa.JSON(), nullable=True),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.Column("updated_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["report_draft_id"],
            ["report_drafts.id"],
            name=op.f("fk_report_sections_report_draft_id_report_drafts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_sections")),
        sa.UniqueConstraint(
            "report_draft_id",
            "section_key",
            "version",
            name=op.f("uq_report_sections_report_draft_id"),
        ),
    )
    op.create_index(
        "ix_report_sections_draft_key_digest",
        "report_sections",
        ["report_draft_id", "section_key", "content_digest"],
        unique=False,
    )
    op.create_table(
        "repository_contents",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("repository_id", UINT, nullable=False),
        sa.Column("content_type", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String(length=512), nullable=False),
        sa.Column("content", medium_text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("fetched_at", utc_datetime(), nullable=False),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.Column("updated_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_repository_contents_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repository_contents")),
        sa.UniqueConstraint(
            "repository_id",
            "content_type",
            name=op.f("uq_repository_contents_repository_id"),
        ),
    )
    for table_name, constraint_name, expression in CHECKS:
        op.execute(
            sa.text(
                f"ALTER TABLE `{table_name}` "
                f"ADD CONSTRAINT `{constraint_name}` CHECK ({expression})"
            )
        )


def downgrade() -> None:
    """Drop the Agent, report, and content cache tables."""
    op.drop_table("repository_contents")
    op.drop_table("report_sections")
    op.drop_table("report_drafts")
    op.drop_table("report_sources")
    op.drop_table("reports")
    op.drop_table("tool_calls")
    op.drop_table("agent_steps")
    op.drop_table("agent_runs")
    op.drop_table("agent_configs")
