"""add agent run reviews

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
    """Return the project MySQL UTC DATETIME type."""
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def uint64() -> sa.BigInteger:
    """Return the project unsigned BIGINT type.

    A plain `sa.BigInteger` here fails on MySQL with error 3780: the referencing column comes out
    signed while `agent_runs.id` is unsigned. The message points at the column, not the type, so
    the alias is the fix.
    """
    return sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def _has_table(table_name: str) -> bool:
    """Return whether the table already exists."""
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
    """Create the table that records what a human concluded about a run."""
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
    """Drop the run-review table."""
    if _has_table("agent_run_reviews"):
        op.drop_table("agent_run_reviews")
