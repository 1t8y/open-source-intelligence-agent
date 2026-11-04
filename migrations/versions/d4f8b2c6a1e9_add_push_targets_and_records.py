"""新增推送目标、推送记录与 push 运行类型

`push_targets` 是唯一允许的收件地址清单，`report_pushes` 是投递台账；`pipeline_runs`
的运行类型枚举同时接纳 `push`。

Revision ID: d4f8b2c6a1e9
Revises: b7d2f4a91c63
Create Date: 2026-10-13 17:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "d4f8b2c6a1e9"
down_revision: str | None = "b7d2f4a91c63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_TYPE_CHECK = "ck_pipeline_runs_run_type_valid"
RUN_TYPES_WITH_PUSH = "'collect', 'snapshot', 'rank', 'agent', 'publish', 'push'"
RUN_TYPES_WITHOUT_PUSH = "'collect', 'snapshot', 'rank', 'agent', 'publish'"


def utc_datetime() -> sa.DateTime:
    """返回项目统一的 MySQL UTC `DATETIME` 类型。"""
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def uint64() -> sa.BigInteger:
    """返回项目统一的无符号 `BIGINT` 类型。"""
    return sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def _has_check(table_name: str, constraint_name: str) -> bool:
    """返回某个 `CHECK` 约束是否已经存在。"""
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


def upgrade() -> None:
    """创建推送白名单与投递台账，并登记 `push` 运行类型。

    `push_targets` 是白名单：报告只允许从这些地址发出去。`report_pushes` 是投递台账，
    以 `idempotency_key` 去重，使同一组 (报告, 版本, 目标) 至多投递一次，重试时推进
    既有行，而不是再添一行。
    """
    op.create_table(
        "push_targets",
        sa.Column("id", uint64(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("destination", sa.String(length=512), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("allow_private", sa.Boolean(), nullable=False),
        sa.Column("allow_confidential", sa.Boolean(), nullable=False),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.Column("updated_at", utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_push_targets")),
        sa.UniqueConstraint("name", name=op.f("uq_push_targets_name")),
    )
    op.execute(
        sa.text(
            "ALTER TABLE `push_targets` "
            "ADD CONSTRAINT `ck_push_targets_channel_valid` "
            "CHECK (`channel` IN ('email', 'webhook'))"
        )
    )
    op.create_index(
        "ix_push_targets_channel_enabled",
        "push_targets",
        ["channel", "enabled"],
        unique=False,
    )

    op.create_table(
        "report_pushes",
        sa.Column("id", uint64(), autoincrement=True, nullable=False),
        sa.Column("report_id", uint64(), nullable=False),
        sa.Column("target_id", uint64(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("destination", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("message_json", sa.JSON(), nullable=True),
        sa.Column("response_excerpt", sa.String(length=512), nullable=True),
        sa.Column("pipeline_run_id", uint64(), nullable=True),
        sa.Column("sent_at", utc_datetime(), nullable=True),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.Column("updated_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["pipeline_runs.id"],
            name=op.f("fk_report_pushes_pipeline_run_id_pipeline_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["reports.id"],
            name=op.f("fk_report_pushes_report_id_reports"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["push_targets.id"],
            name=op.f("fk_report_pushes_target_id_push_targets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_pushes")),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_report_pushes_idempotency_key"),
        ),
    )
    op.execute(
        sa.text(
            "ALTER TABLE `report_pushes` "
            "ADD CONSTRAINT `ck_report_pushes_status_valid` "
            "CHECK (`status` IN ('pending', 'sent', 'failed'))"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE `report_pushes` "
            "ADD CONSTRAINT `ck_report_pushes_version_positive` CHECK (`version` >= 1)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE `report_pushes` "
            "ADD CONSTRAINT `ck_report_pushes_attempt_non_negative` CHECK (`attempt` >= 0)"
        )
    )
    op.create_index(
        "ix_report_pushes_report_version",
        "report_pushes",
        ["report_id", "version"],
        unique=False,
    )
    op.create_index(
        "ix_report_pushes_status_updated",
        "report_pushes",
        ["status", "updated_at"],
        unique=False,
    )

    op.execute(sa.text(f"ALTER TABLE `pipeline_runs` DROP CHECK `{RUN_TYPE_CHECK}`"))
    op.execute(
        sa.text(
            f"ALTER TABLE `pipeline_runs` ADD CONSTRAINT `{RUN_TYPE_CHECK}` "
            f"CHECK (`run_type` IN ({RUN_TYPES_WITH_PUSH}))"
        )
    )


def downgrade() -> None:
    """删除推送相关表，并注销 `push` 运行类型。"""
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "report_pushes" in existing:
        op.drop_table("report_pushes")
    if "push_targets" in existing:
        op.drop_table("push_targets")

    # 收窄后的 `CHECK` 会拒绝本次修订放行的运行行，所以先把它们删掉。
    op.execute(sa.text("DELETE FROM `pipeline_runs` WHERE `run_type` = 'push'"))
    if _has_check("pipeline_runs", RUN_TYPE_CHECK):
        op.execute(sa.text(f"ALTER TABLE `pipeline_runs` DROP CHECK `{RUN_TYPE_CHECK}`"))
    op.execute(
        sa.text(
            f"ALTER TABLE `pipeline_runs` ADD CONSTRAINT `{RUN_TYPE_CHECK}` "
            f"CHECK (`run_type` IN ({RUN_TYPES_WITHOUT_PUSH}))"
        )
    )
