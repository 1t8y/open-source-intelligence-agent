"""新增不可变的报告版本

每次发布都留一份内容不可改的历史，供回滚与审计比对。

Revision ID: d9e4b7c1a2f3
Revises: c4a9e2f1d6b8
Create Date: 2026-10-12 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "d9e4b7c1a2f3"
down_revision: str | None = "c4a9e2f1d6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def utc_datetime() -> sa.DateTime:
    """返回项目统一的 MySQL UTC `DATETIME` 类型。"""
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    """创建不可变的报告版本表。"""
    op.create_table(
        "report_versions",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "report_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "content_markdown",
            sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=True,
        ),
        sa.Column("change_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_report_versions_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["reports.id"],
            name=op.f("fk_report_versions_report_id_reports"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_versions")),
        sa.UniqueConstraint(
            "report_id",
            "version",
            name=op.f("uq_report_versions_report_id"),
        ),
    )
    op.execute(
        sa.text(
            "ALTER TABLE `report_versions` ADD CONSTRAINT `ck_report_versions_version_positive` "
            "CHECK (`version` >= 1)"
        )
    )
    op.create_index(
        "ix_report_versions_report_created",
        "report_versions",
        ["report_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """删除不可变的报告版本表。"""
    op.drop_index("ix_report_versions_report_created", table_name="report_versions")
    op.execute(
        sa.text("ALTER TABLE `report_versions` DROP CHECK `ck_report_versions_version_positive`")
    )
    op.drop_table("report_versions")
