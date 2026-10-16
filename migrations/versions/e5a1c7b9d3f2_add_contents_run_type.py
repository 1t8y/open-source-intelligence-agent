"""add the contents run type

Revision ID: e5a1c7b9d3f2
Revises: c8b4e6f2a013
Create Date: 2026-10-16 23:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a1c7b9d3f2"
down_revision: str | None = "c8b4e6f2a013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_TYPE_CHECK = "ck_pipeline_runs_run_type_valid"
TOPIC_SCOPE_CHECK = "ck_pipeline_runs_topic_scope_valid"
RUN_TYPES_WITH_CONTENTS = (
    "'collect', 'snapshot', 'rank', 'contents', 'backfill', 'agent', 'publish', 'push'"
)
RUN_TYPES_WITHOUT_CONTENTS = "'collect', 'snapshot', 'rank', 'backfill', 'agent', 'publish', 'push'"
GLOBAL_WITH_CONTENTS = "'collect', 'snapshot', 'rank', 'contents', 'backfill'"
GLOBAL_WITHOUT_CONTENTS = "'collect', 'snapshot', 'rank', 'backfill'"


def upgrade() -> None:
    """Register `contents` as a global pipeline run type.

    The `contents` stage pre-warms the `repository_contents` cache for the ranked candidates,
    and it belongs in the same ledger as every other stage: without a run row, "did the cache
    refresh today, and did anything fail" would only be answerable from container logs.

    It is a *global* run type, which is why both CHECK constraints move. `run_type_valid` gains
    the value; `topic_scope_valid` gains it too, but for the opposite reason - it lists the types
    that are allowed to have `topic_id IS NULL`. A type that only appears in the first CHECK
    would be rejected at insert time by the second, which is the failure mode this pair exists to
    prevent.
    """
    op.execute(sa.text(f"ALTER TABLE `pipeline_runs` DROP CHECK `{RUN_TYPE_CHECK}`"))
    op.execute(
        sa.text(
            f"ALTER TABLE `pipeline_runs` ADD CONSTRAINT `{RUN_TYPE_CHECK}` "
            f"CHECK (`run_type` IN ({RUN_TYPES_WITH_CONTENTS}))"
        )
    )
    op.execute(sa.text(f"ALTER TABLE `pipeline_runs` DROP CHECK `{TOPIC_SCOPE_CHECK}`"))
    op.execute(
        sa.text(
            f"ALTER TABLE `pipeline_runs` ADD CONSTRAINT `{TOPIC_SCOPE_CHECK}` "
            f"CHECK (`run_type` IN ({GLOBAL_WITH_CONTENTS}) OR `topic_id` IS NOT NULL)"
        )
    )


def downgrade() -> None:
    """Unregister `contents`, dropping the run rows the narrower CHECK would reject."""
    op.execute(sa.text("DELETE FROM `pipeline_runs` WHERE `run_type` = 'contents'"))
    op.execute(sa.text(f"ALTER TABLE `pipeline_runs` DROP CHECK `{TOPIC_SCOPE_CHECK}`"))
    op.execute(
        sa.text(
            f"ALTER TABLE `pipeline_runs` ADD CONSTRAINT `{TOPIC_SCOPE_CHECK}` "
            f"CHECK (`run_type` IN ({GLOBAL_WITHOUT_CONTENTS}) OR `topic_id` IS NOT NULL)"
        )
    )
    op.execute(sa.text(f"ALTER TABLE `pipeline_runs` DROP CHECK `{RUN_TYPE_CHECK}`"))
    op.execute(
        sa.text(
            f"ALTER TABLE `pipeline_runs` ADD CONSTRAINT `{RUN_TYPE_CHECK}` "
            f"CHECK (`run_type` IN ({RUN_TYPES_WITHOUT_CONTENTS}))"
        )
    )
