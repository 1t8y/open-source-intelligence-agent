"""创建核心表结构

建立后续所有阶段共用的基础表：数据源、角色、用户、审计日志、仓库、主题、用户角色关联、
流水线运行、仓库快照与主题候选关联。它们先落地，采集、排名与报告才有统一的主数据和
外键骨架可用。

Revision ID: 45a1c2996181
Revises:
Create Date: 2026-10-09 11:27:49.592470
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


def utc_datetime() -> sa.DateTime:
    """返回本迁移所用的历史 MySQL UTC `DATETIME` 类型。"""
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


revision: str = "45a1c2996181"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """应用本次表结构变更。"""
    # ### 以下命令由 Alembic 自动生成，请按需调整 ###
    op.create_table(
        "data_sources",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=False),
        sa.Column("credential_ref", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=True),
        sa.Column(
            "last_success_at",
            utc_datetime(),
            nullable=True,
        ),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.Column("updated_at", utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_sources")),
        sa.UniqueConstraint("name", name=op.f("uq_data_sources_name")),
    )
    op.create_table(
        "roles",
        sa.Column(
            "id",
            sa.SmallInteger().with_variant(mysql.SMALLINT(unsigned=True), "mysql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("code", name=op.f("uq_roles_code")),
    )
    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("data_scope", sa.String(length=32), nullable=False),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.Column("updated_at", utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
    op.create_table(
        "audit_logs",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=True,
        ),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "ip_address",
            sa.LargeBinary(length=16).with_variant(sa.VARBINARY(length=16), "mysql"),
            nullable=True,
        ),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_audit_logs_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(
        "ix_audit_logs_action_created", "audit_logs", ["action", "created_at"], unique=False
    )
    op.create_index(
        "ix_audit_logs_actor_created", "audit_logs", ["actor_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_audit_logs_resource", "audit_logs", ["resource_type", "resource_id"], unique=False
    )
    op.create_table(
        "repositories",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column("provider_repo_id", sa.String(length=128), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("html_url", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=64), nullable=True),
        sa.Column("license", sa.String(length=128), nullable=True),
        sa.Column("topics_json", sa.JSON(), nullable=True),
        sa.Column("stars", sa.Integer(), nullable=False),
        sa.Column("forks", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", utc_datetime(), nullable=False),
        sa.Column("last_seen_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.id"],
            name=op.f("fk_repositories_source_id_data_sources"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repositories")),
        sa.UniqueConstraint(
            "source_id", "provider_repo_id", name=op.f("uq_repositories_source_id")
        ),
    )
    op.create_index(
        "ix_repositories_language_stars", "repositories", ["language", "stars"], unique=False
    )
    op.create_index("ix_repositories_last_seen", "repositories", ["last_seen_at"], unique=False)
    op.create_table(
        "topics",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("languages_json", sa.JSON(), nullable=True),
        sa.Column("keywords_json", sa.JSON(), nullable=True),
        sa.Column("min_stars", sa.Integer(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("max_candidates", sa.Integer(), nullable=False),
        sa.Column("publish_mode", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_by",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.Column("updated_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_topics_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_topics")),
        sa.UniqueConstraint("name", name=op.f("uq_topics_name")),
    )
    op.create_table(
        "user_roles",
        sa.Column(
            "user_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column(
            "role_id",
            sa.SmallInteger().with_variant(mysql.SMALLINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_user_roles_role_id_roles"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_roles_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "role_id", name=op.f("pk_user_roles")),
    )
    op.create_table(
        "pipeline_runs",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "topic_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column("run_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", utc_datetime(), nullable=True),
        sa.Column("finished_at", utc_datetime(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topics.id"],
            name=op.f("fk_pipeline_runs_topic_id_topics"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_runs")),
    )
    op.create_index("ix_pipeline_runs_started_at", "pipeline_runs", ["started_at"], unique=False)
    op.create_index(
        "ix_pipeline_runs_topic_type_status",
        "pipeline_runs",
        ["topic_id", "run_type", "status"],
        unique=False,
    )
    op.create_table(
        "repo_snapshots",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "repository_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("stars", sa.Integer(), nullable=False),
        sa.Column("forks", sa.Integer(), nullable=False),
        sa.Column("open_issues", sa.Integer(), nullable=True),
        sa.Column("stars_delta", sa.Integer(), nullable=True),
        sa.Column("forks_delta", sa.Integer(), nullable=True),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_repo_snapshots_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repo_snapshots")),
        sa.UniqueConstraint(
            "repository_id", "snapshot_date", name=op.f("uq_repo_snapshots_repository_id")
        ),
    )
    op.create_index(
        "ix_repo_snapshots_date_stars_delta",
        "repo_snapshots",
        ["snapshot_date", "stars_delta"],
        unique=False,
    )
    op.create_table(
        "topic_repositories",
        sa.Column(
            "topic_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column(
            "repository_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column("score", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("selected_for_agent", sa.Boolean(), nullable=False),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_topic_repositories_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topics.id"],
            name=op.f("fk_topic_repositories_topic_id_topics"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("topic_id", "repository_id", name=op.f("pk_topic_repositories")),
    )
    op.create_index(
        "ix_topic_repositories_topic_rank", "topic_repositories", ["topic_id", "rank"], unique=False
    )
    # ### Alembic 命令结束 ###


def downgrade() -> None:
    """回滚本次表结构变更。"""
    # ### 以下命令由 Alembic 自动生成，请按需调整 ###
    op.drop_table("topic_repositories")
    op.drop_table("repo_snapshots")
    op.drop_table("pipeline_runs")
    op.drop_table("user_roles")
    op.drop_table("topics")
    op.drop_table("repositories")
    op.drop_table("audit_logs")
    op.drop_table("users")
    op.drop_table("roles")
    op.drop_table("data_sources")
    # ### Alembic 命令结束 ###
