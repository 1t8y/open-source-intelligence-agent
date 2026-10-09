"""add unique data source type

Revision ID: 8f2c1d4a6b70
Revises: 3a0741ee1cd5
Create Date: 2026-10-09 13:10:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "8f2c1d4a6b70"
down_revision: str | None = "3a0741ee1cd5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Ensure each provider has only one configured data source row."""
    op.create_unique_constraint(
        "uq_data_sources_source_type",
        "data_sources",
        ["source_type"],
    )


def downgrade() -> None:
    """Remove the provider uniqueness constraint."""
    op.drop_constraint(
        "uq_data_sources_source_type",
        "data_sources",
        type_="unique",
    )
