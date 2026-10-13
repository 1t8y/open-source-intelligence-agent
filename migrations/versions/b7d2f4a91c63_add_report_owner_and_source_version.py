"""add report owner and versioned report sources

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
    """Return the project unsigned BIGINT type."""
    return sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def upgrade() -> None:
    """Add the report owner and make report citations version-scoped.

    `owner_id` is what gives the `PRIVATE` visibility a meaning; existing rows keep
    `owner_id = NULL` and therefore stay `ADMIN`-only, the safe reading of "private" for a
    report whose owner was never recorded.

    `report_sources.version` moves the citation set from "the current version" to "this
    version". Rolling back to an earlier immutable version has to restore the evidence that
    backed that version, and `INV-10` has to be checkable against the version being
    published. Existing rows are backfilled to their report's current version, which is
    exactly what they described before this revision.
    """
    op.add_column("reports", sa.Column("owner_id", uint64(), nullable=True))
    op.create_foreign_key(
        op.f("fk_reports_owner_id_users"),
        "reports",
        "users",
        ["owner_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_reports_owner_status",
        "reports",
        ["owner_id", "status"],
        unique=False,
    )

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
    op.execute(
        sa.text(
            "ALTER TABLE `report_sources` "
            "ADD CONSTRAINT `ck_report_sources_version_positive` CHECK (`version` >= 1)"
        )
    )
    op.drop_index("ix_report_sources_report_section", table_name="report_sources")
    op.create_index(
        "ix_report_sources_report_section",
        "report_sources",
        ["report_id", "version", "section_key"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the report owner column and collapse citations back onto the report."""
    op.drop_index("ix_report_sources_report_section", table_name="report_sources")
    op.create_index(
        "ix_report_sources_report_section",
        "report_sources",
        ["report_id", "section_key"],
        unique=False,
    )
    op.execute(
        sa.text("ALTER TABLE `report_sources` DROP CHECK `ck_report_sources_version_positive`")
    )
    op.drop_column("report_sources", "version")

    op.drop_index("ix_reports_owner_status", table_name="reports")
    op.drop_constraint(op.f("fk_reports_owner_id_users"), "reports", type_="foreignkey")
    op.drop_column("reports", "owner_id")
