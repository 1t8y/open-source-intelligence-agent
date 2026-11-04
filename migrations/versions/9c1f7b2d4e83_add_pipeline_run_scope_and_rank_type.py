"""允许全局流水线运行并新增 rank 运行类型

把 `pipeline_runs.topic_id` 改为可空，采集、快照、排名这些不依附单一主题的阶段才能
记录运行；同时把 `rank` 加入 `run_type` 枚举，并用新的主题范围约束限定哪些类型可以
没有 `topic_id` —— 只有全局阶段才允许为空。

Revision ID: 9c1f7b2d4e83
Revises: 8f2c1d4a6b70
Create Date: 2026-10-12 17:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "9c1f7b2d4e83"
down_revision: str | None = "8f2c1d4a6b70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_TYPE_CHECK = "ck_pipeline_runs_run_type_valid"
TOPIC_SCOPE_CHECK = "ck_pipeline_runs_topic_scope_valid"


def upgrade() -> None:
    """允许不带主题的采集、快照与排名运行，并新增 `rank` 运行类型。"""
    op.alter_column(
        "pipeline_runs",
        "topic_id",
        existing_type=mysql.BIGINT(unsigned=True),
        nullable=True,
    )
    op.execute(sa.text(f"ALTER TABLE `pipeline_runs` DROP CHECK `{RUN_TYPE_CHECK}`"))
    op.execute(
        sa.text(
            f"ALTER TABLE `pipeline_runs` ADD CONSTRAINT `{RUN_TYPE_CHECK}` "
            "CHECK (`run_type` IN ('collect', 'snapshot', 'rank', 'agent', 'publish'))"
        )
    )
    op.execute(
        sa.text(
            f"ALTER TABLE `pipeline_runs` ADD CONSTRAINT `{TOPIC_SCOPE_CHECK}` "
            "CHECK (`run_type` IN ('collect', 'snapshot', 'rank') OR `topic_id` IS NOT NULL)"
        )
    )


def downgrade() -> None:
    """恢复按主题限定运行类型的约束。"""
    op.execute(sa.text(f"ALTER TABLE `pipeline_runs` DROP CHECK `{TOPIC_SCOPE_CHECK}`"))
    op.execute(sa.text(f"ALTER TABLE `pipeline_runs` DROP CHECK `{RUN_TYPE_CHECK}`"))
    op.execute(
        sa.text("DELETE FROM `pipeline_runs` WHERE `topic_id` IS NULL"),
    )
    # MySQL 拒绝修改仍被外键引用的列，所以先删外键，改完可空性再加回来。
    op.drop_constraint(
        "fk_pipeline_runs_topic_id_topics",
        "pipeline_runs",
        type_="foreignkey",
    )
    op.alter_column(
        "pipeline_runs",
        "topic_id",
        existing_type=mysql.BIGINT(unsigned=True),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_pipeline_runs_topic_id_topics",
        "pipeline_runs",
        "topics",
        ["topic_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        sa.text(
            f"ALTER TABLE `pipeline_runs` ADD CONSTRAINT `{RUN_TYPE_CHECK}` "
            "CHECK (`run_type` IN ('collect', 'snapshot', 'agent', 'publish'))"
        )
    )
