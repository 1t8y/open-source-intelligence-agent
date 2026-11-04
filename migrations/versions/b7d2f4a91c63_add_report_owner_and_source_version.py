"""新增报告所有者，并让报告引用按版本归属

给 `reports` 增加 `owner_id`，让 `PRIVATE` 可见性有明确归属；给 `report_sources`
增加 `version`，使引用集合跟着不可变版本走。

Revision ID: b7d2f4a91c63
Revises: e5b1c8a3f7d2
Create Date: 2026-10-13 15:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "b7d2f4a91c63"
down_revision: str | None = "e5b1c8a3f7d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def uint64() -> sa.BigInteger:
    """返回项目统一的无符号 `BIGINT` 类型。"""
    return sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def _has_column(table_name: str, column_name: str) -> bool:
    """返回该列是否已经存在。"""
    return column_name in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _has_index(table_name: str, index_name: str) -> bool:
    """返回该索引是否已经存在。"""
    return index_name in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def _index_columns(table_name: str, index_name: str) -> list[str] | None:
    """返回某个索引的列顺序；索引不存在时返回 `None`。"""
    for index in sa.inspect(op.get_bind()).get_indexes(table_name):
        if index["name"] == index_name:
            return [str(column) for column in index["column_names"]]
    return None


def _has_constraint(table_name: str, constraint_name: str) -> bool:
    """返回具名的 `CHECK` 或外键约束是否存在。"""
    value = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM information_schema.table_constraints "
                "WHERE constraint_schema = DATABASE() AND table_name = :table_name "
                "AND constraint_name = :constraint_name"
            ),
            {"table_name": table_name, "constraint_name": constraint_name},
        )
        .scalar_one()
    )
    return bool(value)


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    """索引存在时才删除。"""
    if _has_index(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)


def _drop_constraint_if_exists(table_name: str, constraint_name: str) -> None:
    """外键约束存在时才删除。"""
    if _has_constraint(table_name, constraint_name):
        op.drop_constraint(constraint_name, table_name, type_="foreignkey")


def upgrade() -> None:
    """新增报告所有者，并让报告引用按版本归档。

    `owner_id` 才让 `PRIVATE` 可见性有实际含义；已有行保持 `owner_id = NULL`，因而
    仍然只有 `ADMIN` 可见 —— 对于从未记录过所有者的报告，这是对「私有」最安全的读法。

    `report_sources.version` 把引用集合从「当前版本」挪到「本版本」。回滚到更早的
    不可变版本时，必须能还原支撑那个版本的证据；`INV-10` 也必须能针对正在发布的
    版本校验。已有行按所属报告的当前版本回填，这正是本次修订之前它们所描述的内容。
    """
    owner_fk = op.f("fk_reports_owner_id_users")
    if not _has_column("reports", "owner_id"):
        op.add_column("reports", sa.Column("owner_id", uint64(), nullable=True))
    if not _has_constraint("reports", owner_fk):
        op.create_foreign_key(
            owner_fk,
            "reports",
            "users",
            ["owner_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not _has_index("reports", "ix_reports_owner_status"):
        op.create_index(
            "ix_reports_owner_status",
            "reports",
            ["owner_id", "status"],
            unique=False,
        )

    if not _has_column("report_sources", "version"):
        op.add_column(
            "report_sources",
            sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        )
    op.execute(
        sa.text(
            "UPDATE report_sources AS rs "
            "JOIN reports AS r ON r.id = rs.report_id "
            "SET rs.version = r.current_version"
        )
    )
    op.alter_column("report_sources", "version", server_default=None)
    if not _has_constraint("report_sources", "ck_report_sources_version_positive"):
        op.execute(
            sa.text(
                "ALTER TABLE `report_sources` "
                "ADD CONSTRAINT `ck_report_sources_version_positive` CHECK (`version` >= 1)"
            )
        )

    final_index = "ix_report_sources_report_section"
    temporary_index = "ix_report_sources_report_id_version_section"
    desired_columns = ["report_id", "version", "section_key"]
    if _index_columns("report_sources", final_index) != desired_columns:
        if not _has_index("report_sources", temporary_index):
            op.create_index(
                temporary_index,
                "report_sources",
                desired_columns,
                unique=False,
            )
        _drop_index_if_exists("report_sources", final_index)
        if not _has_index("report_sources", final_index):
            op.create_index(
                final_index,
                "report_sources",
                desired_columns,
                unique=False,
            )
    _drop_index_if_exists("report_sources", temporary_index)


def downgrade() -> None:
    """删除报告所有者列，并把引用收回到报告粒度。"""
    final_index = "ix_report_sources_report_section"
    temporary_index = "ix_report_sources_report_id_section_tmp"
    previous_columns = ["report_id", "section_key"]
    if _index_columns("report_sources", final_index) != previous_columns:
        if not _has_index("report_sources", temporary_index):
            op.create_index(
                temporary_index,
                "report_sources",
                previous_columns,
                unique=False,
            )
        _drop_index_if_exists("report_sources", final_index)
        if not _has_index("report_sources", final_index):
            op.create_index(
                final_index,
                "report_sources",
                previous_columns,
                unique=False,
            )
    _drop_index_if_exists("report_sources", temporary_index)
    if _has_constraint("report_sources", "ck_report_sources_version_positive"):
        op.execute(
            sa.text("ALTER TABLE `report_sources` DROP CHECK `ck_report_sources_version_positive`")
        )
    if _has_column("report_sources", "version"):
        op.drop_column("report_sources", "version")

    _drop_constraint_if_exists("reports", op.f("fk_reports_owner_id_users"))
    _drop_index_if_exists("reports", "ix_reports_owner_status")
    if _has_column("reports", "owner_id"):
        op.drop_column("reports", "owner_id")
