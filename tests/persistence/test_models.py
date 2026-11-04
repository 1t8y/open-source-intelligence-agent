"""守护 ORM 元数据、关系与核心约束。"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.base import Base
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.identity import Role, User, UserRole
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    RepoSnapshot,
)


def build_session() -> Session:
    """为模型用例造一个隔离的内存数据库。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_metadata_contains_core_tables() -> None:
    """证明 Alembic 元数据列出了 T2/T7、T13 推送、T14 评估、
    T16 回填与 T26 分享所需的表。
    """
    assert set(Base.metadata.tables) == {
        "agent_configs",
        "agent_run_reviews",
        "agent_runs",
        "agent_steps",
        "audit_logs",
        "backfill_jobs",
        "data_sources",
        "eval_cases",
        "eval_datasets",
        "eval_results",
        "eval_runs",
        "pipeline_runs",
        "push_targets",
        "repo_snapshots",
        "report_drafts",
        "report_pushes",
        "report_reviews",
        "report_share_links",
        "report_sections",
        "report_sources",
        "report_verifications",
        "report_versions",
        "reports",
        "repositories",
        "repository_contents",
        "roles",
        "tool_calls",
        "topic_repositories",
        "topics",
        "user_roles",
        "users",
    }


def test_user_role_relationship_round_trip() -> None:
    """证明用户与角色经由关联对象正确连起来。"""
    with build_session() as session:
        user = User(
            id=1,
            username="admin",
            password_hash="hash",
            display_name="Admin",
        )
        role = Role(id=1, code="ADMIN", name="Administrator")
        session.add_all([user, role])
        session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id))
        session.commit()

        assert user.role_links[0].role.code == "ADMIN"


def test_snapshot_unique_constraint_is_enforced() -> None:
    """证明同一个仓库每天只能存在一条快照。"""
    with build_session() as session:
        source = DataSource(
            id=1,
            name="GitHub",
            source_type="github",
            base_url="https://api.github.com",
        )
        repository = Repository(
            id=1,
            source_id=source.id,
            provider_repo_id="123",
            full_name="example/repo",
            html_url="https://github.com/example/repo",
            stars=10,
            forks=2,
            first_seen_at=datetime(2026, 10, 9, tzinfo=UTC),
            last_seen_at=datetime(2026, 10, 9, tzinfo=UTC),
        )
        snapshot_date = date(2026, 10, 9)
        session.add_all([source, repository])
        session.flush()
        session.add_all(
            [
                RepoSnapshot(
                    id=1,
                    repository_id=repository.id,
                    snapshot_date=snapshot_date,
                    stars=10,
                    forks=2,
                    created_at=datetime(2026, 10, 9, tzinfo=UTC),
                ),
                RepoSnapshot(
                    id=2,
                    repository_id=repository.id,
                    snapshot_date=snapshot_date,
                    stars=11,
                    forks=2,
                    created_at=datetime(2026, 10, 9, tzinfo=UTC),
                ),
            ]
        )

        with pytest.raises(IntegrityError):
            session.commit()


def test_utc_datetime_is_persisted_and_restored_as_aware() -> None:
    """证明带时区的 UTC 时间戳往返后仍保留时区含义。"""
    with build_session() as session:
        timestamp = datetime(2026, 10, 9, 12, 30, tzinfo=UTC)
        source = DataSource(
            id=2,
            name="Hugging Face",
            source_type="huggingface",
            base_url="https://huggingface.co/api",
            last_success_at=timestamp,
        )
        session.add(source)
        session.commit()
        source_id = source.id
        session.expire_all()

        restored = session.get(DataSource, source_id)
        assert restored is not None
        assert restored.last_success_at == timestamp
        assert restored.last_success_at.tzinfo is UTC


def test_created_at_defaults_are_applied() -> None:
    """证明审计记录会自动拿到带时区的创建时间。"""
    with build_session() as session:
        audit = AuditLog(id=1, action="login", resource_type="user", result="success")
        session.add(audit)
        session.commit()

        assert audit.created_at.tzinfo is UTC


def test_invalid_user_status_is_rejected() -> None:
    """证明数据库约束会拒绝不受支持的用户状态。"""
    with build_session() as session:
        session.add(
            User(
                id=2,
                username="invalid",
                password_hash="hash",
                display_name="Invalid",
                status="unknown",
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()


def test_negative_repository_counters_are_rejected() -> None:
    """证明仓库的计数字段不接受负值。"""
    with build_session() as session:
        source = DataSource(
            id=3,
            name="Invalid Source",
            source_type="github",
            base_url="https://api.github.com",
        )
        repository = Repository(
            id=2,
            source_id=source.id,
            provider_repo_id="negative",
            full_name="example/negative",
            html_url="https://github.com/example/negative",
            stars=-1,
            forks=0,
            first_seen_at=datetime(2026, 10, 9, tzinfo=UTC),
            last_seen_at=datetime(2026, 10, 9, tzinfo=UTC),
        )
        session.add_all([source, repository])

        with pytest.raises(IntegrityError):
            session.commit()
