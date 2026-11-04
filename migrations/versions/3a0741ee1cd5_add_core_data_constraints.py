"""为核心表添加数据约束

给核心表补上显式命名的取值范围约束：枚举列只接受限定值，计数列不得为负，时间列必须
后向有序。MySQL 对未命名的 `CHECK` 生成的名字不稳定，显式命名后，后续迁移才能按名字
删除并重建同一条约束。

Revision ID: 3a0741ee1cd5
Revises: 45a1c2996181
Create Date: 2026-10-09 11:49:44.896677
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3a0741ee1cd5"
down_revision: str | None = "45a1c2996181"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKS = (
    ("users", "ck_users_status_valid", "`status` IN ('active', 'disabled')"),
    ("users", "ck_users_data_scope_valid", "`data_scope` IN ('personal', 'team')"),
    ("roles", "ck_roles_code_valid", "`code` IN ('ADMIN', 'EDITOR', 'VIEWER')"),
    ("audit_logs", "ck_audit_logs_result_valid", "`result` IN ('success', 'denied', 'failed')"),
    (
        "data_sources",
        "ck_data_sources_rate_limit_positive",
        "`rate_limit_per_minute` > 0",
    ),
    ("topics", "ck_topics_min_stars_nonnegative", "`min_stars` >= 0"),
    ("topics", "ck_topics_window_days_positive", "`window_days` > 0"),
    ("topics", "ck_topics_max_candidates_positive", "`max_candidates` > 0"),
    (
        "topics",
        "ck_topics_publish_mode_valid",
        "`publish_mode` IN ('AUTO', 'AUTO_WITH_DELAY', 'MANUAL')",
    ),
    ("repositories", "ck_repositories_stars_nonnegative", "`stars` >= 0"),
    ("repositories", "ck_repositories_forks_nonnegative", "`forks` >= 0"),
    ("repo_snapshots", "ck_repo_snapshots_stars_nonnegative", "`stars` >= 0"),
    ("repo_snapshots", "ck_repo_snapshots_forks_nonnegative", "`forks` >= 0"),
    (
        "repo_snapshots",
        "ck_repo_snapshots_open_issues_nonnegative",
        "`open_issues` IS NULL OR `open_issues` >= 0",
    ),
    ("topic_repositories", "ck_topic_repositories_score_nonnegative", "`score` >= 0"),
    ("topic_repositories", "ck_topic_repositories_rank_positive", "`rank` >= 1"),
    (
        "pipeline_runs",
        "ck_pipeline_runs_run_type_valid",
        "`run_type` IN ('collect', 'snapshot', 'agent', 'publish')",
    ),
    (
        "pipeline_runs",
        "ck_pipeline_runs_status_valid",
        "`status` IN ('queued', 'running', 'succeeded', 'failed', 'partial')",
    ),
    ("pipeline_runs", "ck_pipeline_runs_attempt_positive", "`attempt` >= 1"),
    (
        "pipeline_runs",
        "ck_pipeline_runs_time_order_valid",
        "`finished_at` IS NULL OR `started_at` IS NULL OR `finished_at` >= `started_at`",
    ),
)


def upgrade() -> None:
    """以稳定名称应用显式的 MySQL `CHECK` 约束。"""
    for table_name, constraint_name, expression in CHECKS:
        op.execute(
            sa.text(
                f"ALTER TABLE `{table_name}` "
                f"ADD CONSTRAINT `{constraint_name}` CHECK ({expression})"
            )
        )


def downgrade() -> None:
    """按相反顺序删除这些约束。"""
    for table_name, constraint_name, _ in reversed(CHECKS):
        op.execute(sa.text(f"ALTER TABLE `{table_name}` DROP CHECK `{constraint_name}`"))
