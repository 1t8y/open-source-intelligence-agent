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


def upgrade() -> None:
    """Create the Verifier verdict and human review tables."""
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
    op.execute(
        sa.text(
            "ALTER TABLE `report_verifications` "
            "ADD CONSTRAINT `ck_report_verifications_verdict_valid` "
            "CHECK (`verdict` IN ('passed', 'failed'))"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE `report_verifications` "
            "ADD CONSTRAINT `ck_report_verifications_version_positive` CHECK (`version` >= 1)"
        )
    )
    op.create_index(
        "ix_report_verifications_report_created",
        "report_verifications",
        ["report_id", "created_at"],
        unique=False,
    )

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
    op.execute(
        sa.text(
            "ALTER TABLE `report_reviews` "
            "ADD CONSTRAINT `ck_report_reviews_decision_valid` "
            "CHECK (`decision` IN ('approve', 'reject', 'request_changes'))"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE `report_reviews` "
            "ADD CONSTRAINT `ck_report_reviews_version_positive` CHECK (`version` >= 1)"
        )
    )
    op.create_index(
        "ix_report_reviews_report_created",
        "report_reviews",
        ["report_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the Verifier verdict and human review tables."""
    op.drop_index("ix_report_reviews_report_created", table_name="report_reviews")
    op.execute(
        sa.text("ALTER TABLE `report_reviews` DROP CHECK `ck_report_reviews_version_positive`")
    )
    op.execute(
        sa.text("ALTER TABLE `report_reviews` DROP CHECK `ck_report_reviews_decision_valid`")
    )
    op.drop_table("report_reviews")

    op.drop_index(
        "ix_report_verifications_report_created",
        table_name="report_verifications",
    )
    op.execute(
        sa.text(
            "ALTER TABLE `report_verifications` "
            "DROP CHECK `ck_report_verifications_version_positive`"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE `report_verifications` DROP CHECK `ck_report_verifications_verdict_valid`"
        )
    )
    op.drop_table("report_verifications")
