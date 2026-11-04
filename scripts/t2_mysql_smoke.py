"""针对真实 MySQL 数据库验证 T2 的 ORM 映射与约束。

脚本先建用户、角色、数据源、主题、仓库、快照、候选与审计各一行，提交后回读关联属性；
再故意写入重复快照与非法状态，确认数据库把两者都拒掉。运行前需导出 `DATABASE_URL`，
通过判据是末尾打印 `t2_mysql_smoke=ok`，且两条越界写入都输出 `=rejected`。
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.identity import Role, User, UserRole
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    PipelineRun,
    Repository,
    RepoSnapshot,
    Topic,
    TopicRepository,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)


def expect_integrity_error(session: Session, label: str) -> None:
    """断言当前工作单元的提交会违反一条约束。"""
    try:
        session.commit()
    except (IntegrityError, OperationalError):
        session.rollback()
        print(f"{label}=rejected")
        return
    raise AssertionError(f"{label} was accepted unexpectedly")


def main() -> None:
    """跑一遍有代表性的 MySQL 写入、查询与约束冒烟。"""
    database_url = os.environ["DATABASE_URL"]
    engine = create_database_engine(database_url)
    session_factory = create_session_factory(engine)
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

    with session_factory() as session:
        user = User(username="smoke-admin", password_hash="hash", display_name="Smoke Admin")
        role = Role(code="ADMIN", name="Administrator")
        source = DataSource(
            name="GitHub Smoke",
            source_type="github",
            base_url="https://api.github.com",
        )
        session.add_all([user, role, source])
        session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id))

        topic = Topic(
            name="T2 Smoke Topic",
            keywords_json=["agent", "llm"],
            publish_mode="MANUAL",
            created_by=user.id,
        )
        repository = Repository(
            source_id=source.id,
            provider_repo_id="smoke-1",
            full_name="example/smoke",
            html_url="https://github.com/example/smoke",
            topics_json=["agent"],
            stars=10,
            forks=2,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add_all([topic, repository])
        session.flush()
        session.add_all(
            [
                RepoSnapshot(
                    repository_id=repository.id,
                    snapshot_date=date(2026, 10, 9),
                    stars=10,
                    forks=2,
                ),
                TopicRepository(
                    topic_id=topic.id,
                    repository_id=repository.id,
                    score=1,
                    rank=1,
                    selected_for_agent=True,
                ),
                PipelineRun(
                    topic_id=topic.id,
                    run_type="collect",
                    status="succeeded",
                    attempt=1,
                ),
                AuditLog(
                    actor_id=user.id,
                    action="smoke.run",
                    resource_type="topic",
                    resource_id=str(topic.id),
                    result="success",
                ),
            ]
        )
        session.commit()

        assert user.role_links[0].role.code == "ADMIN"
        assert repository.snapshots[0].stars == 10
        audit = session.query(AuditLog).one()
        assert audit.created_at.tzinfo is UTC

        session.add(
            RepoSnapshot(
                repository_id=repository.id,
                snapshot_date=date(2026, 10, 9),
                stars=11,
                forks=2,
            )
        )
        expect_integrity_error(session, "duplicate_snapshot")

        session.add(
            User(
                username="invalid-status",
                password_hash="hash",
                display_name="Invalid",
                status="unknown",
            )
        )
        expect_integrity_error(session, "invalid_user_status")

    engine.dispose()
    print("t2_mysql_smoke=ok")


if __name__ == "__main__":
    main()
