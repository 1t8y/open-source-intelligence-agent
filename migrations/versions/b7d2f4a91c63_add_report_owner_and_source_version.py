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


def _has_column(table_name: str, column_name: str) -> bool:
    """Return whether a column already exists."""
    return column_name in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _has_index(table_name: str, index_name: str) -> bool:
    """Return whether an index already exists."""
    return index_name in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def _index_columns(table_name: str, index_name: str) -> list[str] | None:
    """Return one index's column order, or None when the index is absent."""
    for index in sa.inspect(op.get_bind()).get_indexes(table_name):
        if index["name"] == index_name:
            return [str(column) for column in index["column_names"]]
    return None


def _has_constraint(table_name: str, constraint_name: str) -> bool:
    """Return whether a named check or foreign-key constraint exists."""
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
    """Drop an index only when it is present."""
    if _has_index(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)


def _drop_constraint_if_exists(table_name: str, constraint_name: str) -> None:
    """Drop a foreign-key constraint only when it is present."""
    if _has_constraint(table_name, constraint_name):
        op.drop_constraint(constraint_name, table_name, type_="foreignkey")


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
    """Remove the report owner column and collapse citations back onto the report."""
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
