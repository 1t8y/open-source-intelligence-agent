"""新增 Agent Token 预算与计数

给 `agent_configs` 加 `max_tokens` 预算，给 `agent_runs` 加 prompt 与 completion 计数：
一次运行花掉多少 Token 既能被约束，也能被记录。

Revision ID: c4a9e2f1d6b8
Revises: b3f6a1c2d4e7
Create Date: 2026-10-12 22:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a9e2f1d6b8"
down_revision: str | None = "b3f6a1c2d4e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为 Agent 运行增加 Token 预算与实际消耗计数。"""
    op.add_column(
        "agent_configs",
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="12000"),
    )
    op.add_column(
        "agent_runs",
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agent_runs",
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        sa.text(
            "ALTER TABLE `agent_configs` ADD CONSTRAINT `ck_agent_configs_max_tokens_positive` "
            "CHECK (`max_tokens` > 0)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE `agent_runs` ADD CONSTRAINT `ck_agent_runs_prompt_tokens_nonnegative` "
            "CHECK (`prompt_tokens` >= 0)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE `agent_runs` ADD CONSTRAINT `ck_agent_runs_completion_tokens_nonnegative` "
            "CHECK (`completion_tokens` >= 0)"
        )
    )


def downgrade() -> None:
    """从 Agent 持久化中移除 Token 预算与计数。"""
    op.execute(
        sa.text("ALTER TABLE `agent_runs` DROP CHECK `ck_agent_runs_completion_tokens_nonnegative`")
    )
    op.execute(
        sa.text("ALTER TABLE `agent_runs` DROP CHECK `ck_agent_runs_prompt_tokens_nonnegative`")
    )
    op.execute(
        sa.text("ALTER TABLE `agent_configs` DROP CHECK `ck_agent_configs_max_tokens_positive`")
    )
    op.drop_column("agent_runs", "completion_tokens")
    op.drop_column("agent_runs", "prompt_tokens")
    op.drop_column("agent_configs", "max_tokens")
