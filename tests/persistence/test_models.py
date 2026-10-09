"""Tests for ORM metadata, relationships, and core constraints."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.base import Base
from open_source_intelligence.persistence.models.identity import Role, User, UserRole
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    RepoSnapshot,
)


def build_session() -> Session:
    """Create an isolated in-memory database for model tests."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_metadata_contains_core_tables() -> None:
    """Alembic metadata must expose the ten core T2 tables."""
    assert set(Base.metadata.tables) == {
        "audit_logs",
        "data_sources",
        "pipeline_runs",
        "repo_snapshots",
        "repositories",
        "roles",
        "topic_repositories",
        "topics",
        "user_roles",
        "users",
    }


def test_user_role_relationship_round_trip() -> None:
    """Users and roles must be linked through their association object."""
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
    """Only one daily snapshot may exist for the same repository."""
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
    """Aware UTC timestamps must round-trip without losing timezone meaning."""
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
