"""新增 Agent 运行复核表

记录人工对一次 `agent_runs` 结论给出的判定与备注，让「机器跑完但结果不可信」的情况
有地方落账，而不是只留在聊天记录里。

Revision ID: a1c3e5f7b9d2
Revises: e4c1a9d7b3f5
Create Date: 2026-10-27 21:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "a1c3e5f7b9d2"
down_revision: str | None = "e4c1a9d7b3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def utc_datetime() -> sa.DateTime:
    """返回项目统一的 MySQL UTC `DATETIME` 类型。"""
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def uint64() -> sa.BigInteger:
    """返回项目统一的无符号 `BIGINT` 类型。

    这里若直接写 `sa.BigInteger()`，MySQL 会报 3780：引用列变成有符号，而
    `agent_runs.id` 是无符号。报错指向的是列而不是类型，所以修复要落在类型别名上。
    """
    return sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def _has_table(table_name: str) -> bool:
    """返回该表是否已经存在。"""
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    """返回 MySQL 表上是否已经有该索引。"""
    return index_name in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def _has_check(table_name: str, constraint_name: str) -> bool:
    """返回该 `CHECK` 约束是否已经存在。"""
    value = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM information_schema.table_constraints "
                "WHERE constraint_schema = DATABASE() AND table_name = :table_name "
                "AND constraint_name = :constraint_name AND constraint_type = 'CHECK'"
            ),
            {"table_name": table_name, "constraint_name": constraint_name},
        )
        .scalar_one()
    )
    return bool(value)


def _add_check(table_name: str, constraint_name: str, expression: str) -> None:
    """添加一次 `CHECK` 约束，并容忍 MySQL DDL 只执行一半时的重试。"""
    if _has_check(table_name, constraint_name):
        return
    op.execute(
        sa.text(
            f"ALTER TABLE `{table_name}` ADD CONSTRAINT `{constraint_name}` CHECK ({expression})"
        )
    )


def upgrade() -> None:
    """创建记录人工对一次运行给出何种结论的表。"""
    if not _has_table("agent_run_reviews"):
        op.create_table(
            "agent_run_reviews",
            sa.Column("id", uint64(), autoincrement=True, nullable=False),
            sa.Column("agent_run_id", uint64(), nullable=False),
            sa.Column("reviewer_id", uint64(), nullable=True),
            sa.Column("verdict", sa.String(length=32), nullable=False),
            sa.Column("comment", sa.Text(), nullable=True),
            sa.Column("created_at", utc_datetime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["agent_run_id"],
                ["agent_runs.id"],
                name=op.f("fk_agent_run_reviews_agent_run_id_agent_runs"),
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["reviewer_id"],
                ["users.id"],
                name=op.f("fk_agent_run_reviews_reviewer_id_users"),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_run_reviews")),
        )
    _add_check(
        "agent_run_reviews",
        "ck_agent_run_reviews_verdict_valid",
        "`verdict` IN ('acknowledged', 'needs_rerun')",
    )
    if not _has_index("agent_run_reviews", "ix_agent_run_reviews_run_created"):
        op.create_index(
            "ix_agent_run_reviews_run_created",
            "agent_run_reviews",
            ["agent_run_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    """删除运行复核表。"""
    if _has_table("agent_run_reviews"):
        op.drop_table("agent_run_reviews")
