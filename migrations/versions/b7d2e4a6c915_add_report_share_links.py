"""add report share links

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

#: `reports.id` and `users.id` are BIGINT UNSIGNED. A plain `sa.BigInteger()` is signed, and
#: MySQL refuses the foreign key with error 3780 ("incompatible") - the constraint, not the
#: column, is what fails, so the message points at the column that was correct.
UINT = sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def utc_datetime() -> sa.DateTime:
    """Match `UTCDateTime`: microsecond precision on MySQL, plain DATETIME elsewhere.

    Using a plain `sa.DateTime()` here would produce a MySQL `DATETIME` with the default zero
    fractional seconds, so `last_accessed_at` would lose the sub-second part that the ORM model
    is written to keep.
    """
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    """Create `report_share_links`, the storage behind the no-login report address.

    `token_hash` is `CHAR(64)` with a unique constraint: the hex digest of a sha256 is always
    that length, and the unique index is what turns "look this token up" into a single probe
    instead of a scan. There is deliberately no column for the plaintext token - a database that
    cannot store it cannot leak it.
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
    """Drop the share-link table.

    Dropping it invalidates every link that ever existed; the uptime cost of a downgrade is
    therefore larger than the schema change. That is acceptable for a rollback of the feature
    that created the table, and unacceptable as a routine operation.
    """
    op.drop_index("ix_report_share_links_report_revoked", table_name="report_share_links")
    op.drop_table("report_share_links")
