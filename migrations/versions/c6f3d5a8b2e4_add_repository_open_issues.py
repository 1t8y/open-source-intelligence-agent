"""为仓库增加 open_issues 计数

`repositories` 增加可空的未决 issue 数，快照写入方才有可读的来源。

Revision ID: c6f3d5a8b2e4
Revises: b7e2c4d9f1a3
Create Date: 2026-10-31 16:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6f3d5a8b2e4"
down_revision: str | None = "b7e2c4d9f1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为 `repositories` 增加可空的 `open_issues` 计数。

    `repo_snapshots.open_issues` 自建表起就存在，却从未被填过值：`repositories` 行没有
    这个计数，快照写入方只能硬编码 `None`。本列就是快照写入方现在读取的来源。
    """
    op.add_column(
        "repositories",
        sa.Column("open_issues", sa.Integer(), nullable=True),
    )
    op.execute(
        sa.text(
            "ALTER TABLE `repositories` "
            "ADD CONSTRAINT `ck_repositories_open_issues_nonnegative` "
            "CHECK (`open_issues` IS NULL OR `open_issues` >= 0)"
        )
    )


def downgrade() -> None:
    """删除该计数及其检查约束。"""
    op.execute(
        sa.text(
            "ALTER TABLE `repositories` DROP CONSTRAINT `ck_repositories_open_issues_nonnegative`"
        )
    )
    op.drop_column("repositories", "open_issues")
