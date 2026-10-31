"""add open_issues to repositories

Revision ID: c6f3d5a8b2e4
Revises: b7e2c4d9f1a3
Create Date: 2026-10-31 16:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6f3d5a8b2e4"
down_revision: str | None = "b7e2c4d9f1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable `open_issues` counter to `repositories`.

    The `repo_snapshots.open_issues` column has existed since the schema landed but was never
    populated: the snapshot writer hard-coded None because the `repositories` row carried no
    such counter. This column is the source the snapshot writer now reads from.
    """
    op.add_column(
        "repositories",
        sa.Column("open_issues", sa.Integer(), nullable=True),
    )
    op.execute(
        sa.text(
            "ALTER TABLE `repositories` "
            "ADD CONSTRAINT `ck_repositories_open_issues_nonnegative` "
            "CHECK (`open_issues` IS NULL OR `open_issues` >= 0)"
        )
    )


def downgrade() -> None:
    """Drop the counter and its check constraint."""
    op.execute(
        sa.text(
            "ALTER TABLE `repositories` DROP CONSTRAINT `ck_repositories_open_issues_nonnegative`"
        )
    )
    op.drop_column("repositories", "open_issues")
