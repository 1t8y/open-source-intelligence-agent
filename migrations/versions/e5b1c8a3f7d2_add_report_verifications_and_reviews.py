"""add report verifications and reviews

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
    """Return the project MySQL UTC DATETIME type."""
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def uint64() -> sa.BigInteger:
    """Return the project unsigned BIGINT type."""
    return sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def _has_table(table_name: str) -> bool:
    """Return whether the migration already created one of its tables."""
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    """Return whether an index already exists on a MySQL table."""
    return index_name in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def _has_check(table_name: str, constraint_name: str) -> bool:
    """Return whether a CHECK constraint already exists."""
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
    """Add a CHECK constraint once, tolerating a retry after partial MySQL DDL."""
    if _has_check(table_name, constraint_name):
        return
    op.execute(
        sa.text(
            f"ALTER TABLE `{table_name}` ADD CONSTRAINT `{constraint_name}` CHECK ({expression})"
        )
    )


def upgrade() -> None:
    """Create the Verifier verdict and human review tables."""
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
    """Drop the Verifier verdict and human review tables."""
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name in ("report_reviews", "report_verifications"):
        if table_name in existing:
            op.drop_table(table_name)
