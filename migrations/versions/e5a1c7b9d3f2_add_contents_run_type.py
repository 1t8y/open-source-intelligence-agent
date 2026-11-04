"""新增 contents 运行类型

`contents` 阶段为候选预热 `repository_contents` 缓存，登记为全局运行类型后，缓存
刷新是否发生、是否失败才会出现在台账里。

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
    """把 `contents` 登记为全局流水线运行类型。

    `contents` 阶段为入选候选预热 `repository_contents` 缓存，它应当和所有其它阶段
    记在同一本台账里：没有运行行，「今天缓存刷了吗、有没有失败」就只能去容器日志里找。

    它是*全局*运行类型，所以两个 `CHECK` 约束都要改。`run_type_valid` 增加这个值；
    `topic_scope_valid` 也要增加，但理由相反 —— 后者列出的是允许 `topic_id IS NULL`
    的类型。只出现在第一个 `CHECK` 里的类型，会在插入时被第二个拒绝，这对约束存在的
    意义正是防住这种失败。
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
    """注销 `contents`，先删掉收窄后的 `CHECK` 会拒绝的运行行。"""
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
