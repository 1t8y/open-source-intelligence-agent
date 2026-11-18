"""为主题增加报告类型

给 `topics` 加 `report_type` 列，区分「趋势追踪」（`trend`）与「热门工具追踪」
（`hot_tool`）两种报告类型。默认 `trend` 让既有主题行为不变；研究 Agent 的章节结构、
草稿 Prompt、报告编排与验证都据此分派。

Revision ID: a7d2e9f1b4c3
Revises: c6f3d5a8b2e4
Create Date: 2026-11-19 01:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d2e9f1b4c3"
down_revision: str | None = "c6f3d5a8b2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table_name: str, column_name: str) -> bool:
    """返回该列是否已经存在。"""
    return column_name in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    """新增 `report_type` 列，并用 CHECK 约束限定取值。"""
    if not _has_column("topics", "report_type"):
        op.add_column(
            "topics",
            sa.Column(
                "report_type",
                sa.String(32),
                nullable=False,
                server_default="trend",
            ),
        )
        op.alter_column("topics", "report_type", server_default=None)
    op.execute(
        sa.text(
            "ALTER TABLE `topics` ADD CONSTRAINT `ck_topics_report_type_valid` "
            "CHECK (`report_type` IN ('trend', 'hot_tool'))"
        )
    )


def downgrade() -> None:
    """移除报告类型列与约束。"""
    op.execute(sa.text("ALTER TABLE `topics` DROP CHECK `ck_topics_report_type_valid`"))
    if _has_column("topics", "report_type"):
        op.drop_column("topics", "report_type")
