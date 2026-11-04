"""为数据源类型添加唯一约束

`data_sources` 里每个供应商只允许一行：采集侧按 `source_type` 查配置，出现重复行时
「该用哪一条」就变成了随机选择。

Revision ID: 8f2c1d4a6b70
Revises: 3a0741ee1cd5
Create Date: 2026-10-09 13:10:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "8f2c1d4a6b70"
down_revision: str | None = "3a0741ee1cd5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """确保每个供应商只有一行数据源配置。"""
    op.create_unique_constraint(
        "uq_data_sources_source_type",
        "data_sources",
        ["source_type"],
    )


def downgrade() -> None:
    """移除供应商唯一约束。"""
    op.drop_constraint(
        "uq_data_sources_source_type",
        "data_sources",
        type_="unique",
    )
