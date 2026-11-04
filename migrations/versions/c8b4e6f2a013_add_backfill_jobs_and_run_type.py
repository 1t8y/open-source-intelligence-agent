"""新增可续跑的回填任务与 backfill 运行类型

`backfill_jobs` 逐页记录游标与进度，被中断的遍历可以从下一页继续；`pipeline_runs`
的运行类型枚举同时接纳 `backfill`。

Revision ID: c8b4e6f2a013
Revises: f1a7c9d3b582
Create Date: 2026-10-14 14:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "c8b4e6f2a013"
down_revision: str | None = "f1a7c9d3b582"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_TYPE_CHECK = "ck_pipeline_runs_run_type_valid"
TOPIC_SCOPE_CHECK = "ck_pipeline_runs_topic_scope_valid"
RUN_TYPES_WITH_BACKFILL = "'collect', 'snapshot', 'rank', 'backfill', 'agent', 'publish', 'push'"
RUN_TYPES_WITHOUT_BACKFILL = "'collect', 'snapshot', 'rank', 'agent', 'publish', 'push'"
GLOBAL_WITH_BACKFILL = "'collect', 'snapshot', 'rank', 'backfill'"
GLOBAL_WITHOUT_BACKFILL = "'collect', 'snapshot', 'rank'"

BACKFILL_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "ck_backfill_jobs_status_valid",
        "`status` IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
    ),
    ("ck_backfill_jobs_next_page_positive", "`next_page` >= 1"),
    ("ck_backfill_jobs_target_pages_positive", "`target_pages` >= 1"),
    ("ck_backfill_jobs_per_page_bounded", "`per_page` >= 1 AND `per_page` <= 100"),
    ("ck_backfill_jobs_pages_completed_nonnegative", "`pages_completed` >= 0"),
    ("ck_backfill_jobs_items_collected_nonnegative", "`items_collected` >= 0"),
    ("ck_backfill_jobs_attempt_positive", "`attempt` >= 1"),
    (
        "ck_backfill_jobs_time_order_valid",
        "`finished_at` IS NULL OR `started_at` IS NULL OR `finished_at` >= `started_at`",
    ),
)


def utc_datetime() -> sa.DateTime:
    """返回项目统一的 MySQL UTC `DATETIME` 类型。"""
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def uint64() -> sa.BigInteger:
    """返回项目统一的无符号 `BIGINT` 类型。"""
    return sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def upgrade() -> None:
    """创建回填任务表，并登记 `backfill` 运行类型。

    `backfill_jobs` 是可续跑的工作单元：游标（`next_page`）与两个计数在每页之后提交，
    因此丢失进程的遍历会从未完成的那一页继续，而不是把历史重爬一遍。重放一页不会破坏
    数据 —— 摄取在 `(source, provider_repo_id)` 上是幂等的 —— 但连重放二十页，就会把
    一次网络抖动变成一次重复抓取。

    `backfill` 注册为*全局*运行类型，所以记录遍历的那行不需要 `topic_id`。它和所有
    其它阶段落在同一本 `pipeline_runs` 台账里，遍历因此能在同一处被看到。
    """
    op.create_table(
        "backfill_jobs",
        sa.Column("id", uint64(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("query_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("next_page", sa.Integer(), nullable=False),
        sa.Column("target_pages", sa.Integer(), nullable=False),
        sa.Column("per_page", sa.Integer(), nullable=False),
        sa.Column("pages_completed", sa.Integer(), nullable=False),
        sa.Column("items_collected", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("started_at", utc_datetime(), nullable=True),
        sa.Column("finished_at", utc_datetime(), nullable=True),
        sa.Column("heartbeat_at", utc_datetime(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.Column("updated_at", utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backfill_jobs")),
    )
    for constraint_name, clause in BACKFILL_CHECKS:
        op.execute(
            sa.text(
                f"ALTER TABLE `backfill_jobs` ADD CONSTRAINT `{constraint_name}` CHECK ({clause})"
            )
        )
    op.create_index(
        "ix_backfill_jobs_provider_status",
        "backfill_jobs",
        ["provider", "status"],
        unique=False,
    )
    op.create_index(
        "ix_backfill_jobs_created_at",
        "backfill_jobs",
        ["created_at"],
        unique=False,
    )

    op.execute(sa.text(f"ALTER TABLE `pipeline_runs` DROP CHECK `{RUN_TYPE_CHECK}`"))
    op.execute(
        sa.text(
            f"ALTER TABLE `pipeline_runs` ADD CONSTRAINT `{RUN_TYPE_CHECK}` "
            f"CHECK (`run_type` IN ({RUN_TYPES_WITH_BACKFILL}))"
        )
    )
    op.execute(sa.text(f"ALTER TABLE `pipeline_runs` DROP CHECK `{TOPIC_SCOPE_CHECK}`"))
    op.execute(
        sa.text(
            f"ALTER TABLE `pipeline_runs` ADD CONSTRAINT `{TOPIC_SCOPE_CHECK}` "
            f"CHECK (`run_type` IN ({GLOBAL_WITH_BACKFILL}) OR `topic_id` IS NOT NULL)"
        )
    )


def downgrade() -> None:
    """删除回填任务表，并注销 `backfill` 运行类型。"""
    # 收窄后的 `CHECK` 会拒绝本次修订放行的运行行，所以先把它们删掉。
    op.execute(sa.text("DELETE FROM `pipeline_runs` WHERE `run_type` = 'backfill'"))
    op.execute(sa.text(f"ALTER TABLE `pipeline_runs` DROP CHECK `{TOPIC_SCOPE_CHECK}`"))
    op.execute(
        sa.text(
            f"ALTER TABLE `pipeline_runs` ADD CONSTRAINT `{TOPIC_SCOPE_CHECK}` "
            f"CHECK (`run_type` IN ({GLOBAL_WITHOUT_BACKFILL}) OR `topic_id` IS NOT NULL)"
        )
    )
    op.execute(sa.text(f"ALTER TABLE `pipeline_runs` DROP CHECK `{RUN_TYPE_CHECK}`"))
    op.execute(
        sa.text(
            f"ALTER TABLE `pipeline_runs` ADD CONSTRAINT `{RUN_TYPE_CHECK}` "
            f"CHECK (`run_type` IN ({RUN_TYPES_WITHOUT_BACKFILL}))"
        )
    )
    op.drop_table("backfill_jobs")
