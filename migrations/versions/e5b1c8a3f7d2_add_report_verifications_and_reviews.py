"""新增报告校验与人工评审表

`report_verifications` 存机器校验结论，`report_reviews` 存人工裁决，二者都以
（报告, 版本）为键。

Revision ID: e5b1c8a3f7d2
Revises: d9e4b7c1a2f3
Create Date: 2026-10-13 14:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "e5b1c8a3f7d2"
down_revision: str | None = "d9e4b7c1a2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def utc_datetime() -> sa.DateTime:
    """返回项目统一的 MySQL UTC `DATETIME` 类型。"""
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def uint64() -> sa.BigInteger:
    """返回项目统一的无符号 `BIGINT` 类型。"""
    return sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def _has_table(table_name: str) -> bool:
    """返回本迁移的某张表是否已经创建过。"""
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
    """创建 Verifier 判定表与人工评审表。"""
    if not _has_table("report_verifications"):
        op.create_table(
            "report_verifications",
            sa.Column("id", uint64(), autoincrement=True, nullable=False),
            sa.Column("report_id", uint64(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("verdict", sa.String(length=16), nullable=False),
            sa.Column("structure_ok", sa.Boolean(), nullable=False),
            sa.Column("citations_ok", sa.Boolean(), nullable=False),
            sa.Column("numbers_ok", sa.Boolean(), nullable=False),
            sa.Column("sensitivity_ok", sa.Boolean(), nullable=False),
            sa.Column("reason_codes_json", sa.JSON(), nullable=True),
            sa.Column("details_json", sa.JSON(), nullable=True),
            sa.Column("created_at", utc_datetime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["report_id"],
                ["reports.id"],
                name=op.f("fk_report_verifications_report_id_reports"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_report_verifications")),
            sa.UniqueConstraint(
                "report_id",
                "version",
                name=op.f("uq_report_verifications_report_id"),
            ),
        )
    _add_check(
        "report_verifications",
        "ck_report_verifications_verdict_valid",
        "`verdict` IN ('passed', 'failed')",
    )
    _add_check(
        "report_verifications",
        "ck_report_verifications_version_positive",
        "`version` >= 1",
    )
    if not _has_index("report_verifications", "ix_report_verifications_report_created"):
        op.create_index(
            "ix_report_verifications_report_created",
            "report_verifications",
            ["report_id", "created_at"],
            unique=False,
        )

    if not _has_table("report_reviews"):
        op.create_table(
            "report_reviews",
            sa.Column("id", uint64(), autoincrement=True, nullable=False),
            sa.Column("report_id", uint64(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("reviewer_id", uint64(), nullable=True),
            sa.Column("decision", sa.String(length=32), nullable=False),
            sa.Column("comment", sa.Text(), nullable=True),
            sa.Column("edited_version_id", uint64(), nullable=True),
            sa.Column("created_at", utc_datetime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["edited_version_id"],
                ["report_versions.id"],
                name=op.f("fk_report_reviews_edited_version_id_report_versions"),
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["report_id"],
                ["reports.id"],
                name=op.f("fk_report_reviews_report_id_reports"),
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["reviewer_id"],
                ["users.id"],
                name=op.f("fk_report_reviews_reviewer_id_users"),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_report_reviews")),
        )
    _add_check(
        "report_reviews",
        "ck_report_reviews_decision_valid",
        "`decision` IN ('approve', 'reject', 'request_changes')",
    )
    _add_check(
        "report_reviews",
        "ck_report_reviews_version_positive",
        "`version` >= 1",
    )
    if not _has_index("report_reviews", "ix_report_reviews_report_created"):
        op.create_index(
            "ix_report_reviews_report_created",
            "report_reviews",
            ["report_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    """删除 Verifier 判定表与人工评审表。"""
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name in ("report_reviews", "report_verifications"):
        if table_name in existing:
            op.drop_table(table_name)
