"""为报告推送记录补充目标名称

把推送目标的名称冗余到投递行上，台账不必再读白名单才能显示目标名。

Revision ID: b7e2c4d9f1a3
Revises: a1c3e5f7b9d2
Create Date: 2026-10-27 22:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2c4d9f1a3"
down_revision: str | None = "a1c3e5f7b9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table_name: str, column_name: str) -> bool:
    """返回该列是否已经存在，好让重跑的 DDL 变成空操作。"""
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}
    return column_name in columns


def upgrade() -> None:
    """把目标名称冗余到投递行上，并回填历史数据。

    台账原先要回白名单查名称，而白名单只有 `OPS_CONFIG_ROLES` 能读：于是一个编辑角色
    在目标明明还存在时，每一行都显示「（目标 #N 已不存在）」。把名称抄到行上正是该
    模块一直声称的做法 —— `channel` 与 `destination` 早就出于同一理由做了冗余。
    """
    if not _has_column("report_pushes", "target_name"):
        op.execute(
            sa.text(
                "ALTER TABLE `report_pushes` "
                "ADD COLUMN `target_name` VARCHAR(128) NOT NULL DEFAULT ''"
            )
        )
    # 目标仍能解析的行从白名单回填；外键目标已消失的行留空串，模板于是可以显示
    # 「未知」，而不必凭空编一个名字。
    op.execute(
        sa.text(
            "UPDATE `report_pushes` AS p "
            "JOIN `push_targets` AS t ON t.id = p.target_id "
            "SET p.target_name = t.name "
            "WHERE p.target_name = ''"
        )
    )


def downgrade() -> None:
    """删除冗余的名称列。"""
    if _has_column("report_pushes", "target_name"):
        op.execute(sa.text("ALTER TABLE `report_pushes` DROP COLUMN `target_name`"))
