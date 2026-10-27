"""add report push target name

Revision ID: b7e2c4d9f1a3
Revises: a1c3e5f7b9d2
Create Date: 2026-10-27 22:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2c4d9f1a3"
down_revision: str | None = "a1c3e5f7b9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table_name: str, column_name: str) -> bool:
    """Return whether a column already exists, so a retried DDL run is a no-op."""
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}
    return column_name in columns


def upgrade() -> None:
    """Denormalize the target name onto the delivery row and backfill it.

    The ledger used to resolve the name by looking the row up in the allowlist, which only
    `OPS_CONFIG_ROLES` may read: an editor therefore saw "（目标 #N 已不存在）" on every row
    while the target was alive. Copying the name onto the row is what the module always claimed
    to do — `channel` and `destination` are already denormalized for exactly this reason.
    """
    if not _has_column("report_pushes", "target_name"):
        op.execute(
            sa.text(
                "ALTER TABLE `report_pushes` "
                "ADD COLUMN `target_name` VARCHAR(128) NOT NULL DEFAULT ''"
            )
        )
    # Backfill from the allowlist where the row still resolves; leave '' where the FK target is
    # gone, so the template can say "unknown" without inventing a name.
    op.execute(
        sa.text(
            "UPDATE `report_pushes` AS p "
            "JOIN `push_targets` AS t ON t.id = p.target_id "
            "SET p.target_name = t.name "
            "WHERE p.target_name = ''"
        )
    )


def downgrade() -> None:
    """Drop the denormalized name."""
    if _has_column("report_pushes", "target_name"):
        op.execute(sa.text("ALTER TABLE `report_pushes` DROP COLUMN `target_name`"))
