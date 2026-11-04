"""为主题候选关联增加手工固定标记

用 `manually_pinned` 区分人工挑选与排名产生的关系，避免每日排名把评估用例唯一的
链接清掉。

Revision ID: e4c1a9d7b3f5
Revises: b7d2e4a6c915
Create Date: 2026-10-23 19:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4c1a9d7b3f5"
down_revision: str | None = "b7d2e4a6c915"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table_name: str, column_name: str) -> bool:
    """返回该列是否已经存在。"""
    return column_name in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    """标记那些由人工挑选、而非排名器选出的候选关联。

    排名会按快照窗口重建主题的候选列表：这对受监控的主题是对的，对人工策展的主题则是
    错的 —— 评估主题只保留一条为所测场景挑中的链接，每日排名一跑就会把它丢掉，让用例
    悄悄变空。`manually_pinned` 就是这条区分，存在排名器读得到的地方。已有行默认为
    `False`，即「排名产生」，因此从未人工策展过的主题行为保持不变。
    """
    if not _has_column("topic_repositories", "manually_pinned"):
        op.add_column(
            "topic_repositories",
            sa.Column(
                "manually_pinned",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
        op.alter_column("topic_repositories", "manually_pinned", server_default=None)


def downgrade() -> None:
    """移除手工固定标记。"""
    if _has_column("topic_repositories", "manually_pinned"):
        op.drop_column("topic_repositories", "manually_pinned")
