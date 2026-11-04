"""新增报告分享链接

为免登录阅读报告提供地址：表里只存令牌的 SHA-256 摘要，明文令牌不落库。

Revision ID: b7d2e4a6c915
Revises: e5a1c7b9d3f2
Create Date: 2026-10-19 21:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "b7d2e4a6c915"
down_revision: str | None = "e5a1c7b9d3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `reports.id` 与 `users.id` 都是 `BIGINT UNSIGNED`。直接写 `sa.BigInteger()` 得到的是
#: 有符号类型，MySQL 会以 3780（"incompatible"）拒绝这个外键 —— 失败的是约束而不是列，
#: 所以报错指向的恰恰是本来正确的列。
UINT = sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def utc_datetime() -> sa.DateTime:
    """对齐 `UTCDateTime`：MySQL 上精确到微秒，其它方言用普通 `DATETIME`。

    这里若用朴素的 `sa.DateTime()`，MySQL 会生成小数秒默认为 0 的 `DATETIME`，
    `last_accessed_at` 就会丢掉 ORM 模型刻意保留的亚秒部分。
    """
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    """创建 `report_share_links`，承载免登录报告地址的存储。

    `token_hash` 是带唯一约束的 `CHAR(64)`：sha256 的十六进制摘要长度固定，唯一索引
    把「按令牌查一行」变成一次探测而不是全表扫描。这里刻意不设明文令牌列 ——
    存不下的东西才泄不出去。
    """
    op.create_table(
        "report_share_links",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("report_id", UINT, nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", UINT, nullable=True),
        sa.Column("expires_at", utc_datetime(), nullable=True),
        sa.Column("revoked_at", utc_datetime(), nullable=True),
        sa.Column("last_accessed_at", utc_datetime(), nullable=True),
        sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.Column("updated_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_report_share_links_token_hash"),
    )
    op.create_index(
        "ix_report_share_links_report_revoked",
        "report_share_links",
        ["report_id", "revoked_at"],
    )


def downgrade() -> None:
    """删除分享链接表。

    删表会让所有已发出的链接同时失效，因此这次降级的可用性代价大于表结构本身的变化。
    作为「回滚创建该表的功能」可以接受，作为日常操作则不可接受。
    """
    op.drop_index("ix_report_share_links_report_revoked", table_name="report_share_links")
    op.drop_table("report_share_links")
