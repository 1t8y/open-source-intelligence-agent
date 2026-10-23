"""add a manual pin flag to topic candidate links

Revision ID: e4c1a9d7b3f5
Revises: b7d2e4a6c915
Create Date: 2026-10-23 19:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4c1a9d7b3f5"
down_revision: str | None = "b7d2e4a6c915"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table_name: str, column_name: str) -> bool:
    """Return whether a column already exists."""
    return column_name in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    """Mark candidate links that were chosen by hand rather than by the ranker.

    Ranking rebuilds a topic's candidate list from the snapshot window, which is right for a
    monitored topic and wrong for a curated one: an evaluation topic holds exactly one link
    chosen for the case it exercises, and a daily ranking pass would otherwise drop it and
    silently empty the case. `manually_pinned` is that distinction stored where the ranker can
    read it. Existing rows default to False, i.e. "ranked", which preserves today's behaviour
    for every topic that was never curated by hand.
    """
    if not _has_column("topic_repositories", "manually_pinned"):
        op.add_column(
            "topic_repositories",
            sa.Column(
                "manually_pinned",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
        op.alter_column("topic_repositories", "manually_pinned", server_default=None)


def downgrade() -> None:
    """Remove the manual pin flag."""
    if _has_column("topic_repositories", "manually_pinned"):
        op.drop_column("topic_repositories", "manually_pinned")
