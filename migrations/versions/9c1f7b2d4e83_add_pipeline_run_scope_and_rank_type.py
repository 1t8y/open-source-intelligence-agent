"""add pipeline run scope and rank run type

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
    """Allow global collect, snapshot, and rank runs and add the rank run type."""
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
    """Restore the topic-scoped run type constraint."""
    op.execute(sa.text(f"ALTER TABLE `pipeline_runs` DROP CHECK `{TOPIC_SCOPE_CHECK}`"))
    op.execute(sa.text(f"ALTER TABLE `pipeline_runs` DROP CHECK `{RUN_TYPE_CHECK}`"))
    op.execute(
        sa.text(
            f"ALTER TABLE `pipeline_runs` ADD CONSTRAINT `{RUN_TYPE_CHECK}` "
            "CHECK (`run_type` IN ('collect', 'snapshot', 'agent', 'publish'))"
        )
    )
    op.execute(
        sa.text("DELETE FROM `pipeline_runs` WHERE `topic_id` IS NULL"),
    )
    op.alter_column(
        "pipeline_runs",
        "topic_id",
        existing_type=mysql.BIGINT(unsigned=True),
        nullable=False,
    )
