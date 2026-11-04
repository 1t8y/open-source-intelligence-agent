"""生成仓库的每日快照。"""

from collections.abc import Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.intelligence import Repository, RepoSnapshot
from open_source_intelligence.security.audit import record_audit_event


def create_daily_snapshots(
    session: Session,
    *,
    snapshot_date: date,
) -> int:
    """为给定日期创建或更新每个仓库的一条快照。"""
    repositories = session.scalars(select(Repository).order_by(Repository.id)).all()
    for repository in repositories:
        previous = session.scalar(
            select(RepoSnapshot)
            .where(
                RepoSnapshot.repository_id == repository.id,
                RepoSnapshot.snapshot_date < snapshot_date,
            )
            .order_by(RepoSnapshot.snapshot_date.desc())
            .limit(1)
        )
        current = session.scalar(
            select(RepoSnapshot).where(
                RepoSnapshot.repository_id == repository.id,
                RepoSnapshot.snapshot_date == snapshot_date,
            )
        )
        stars_delta = None if previous is None else repository.stars - previous.stars
        forks_delta = None if previous is None else repository.forks - previous.forks
        if current is None:
            current = RepoSnapshot(
                repository_id=repository.id,
                snapshot_date=snapshot_date,
                stars=repository.stars,
                forks=repository.forks,
                open_issues=repository.open_issues,
                stars_delta=stars_delta,
                forks_delta=forks_delta,
            )
            session.add(current)
        else:
            current.stars = repository.stars
            current.forks = repository.forks
            current.open_issues = repository.open_issues
            current.stars_delta = stars_delta
            current.forks_delta = forks_delta

    record_audit_event(
        session,
        action="snapshot.create",
        resource_type="pipeline",
        result="success",
        metadata_json={
            "snapshot_date": snapshot_date.isoformat(),
            "repository_count": len(repositories),
        },
    )
    session.commit()
    return len(repositories)


def get_snapshot_series(
    session: Session,
    repository_id: int,
    *,
    start_date: date,
    end_date: date,
) -> Sequence[RepoSnapshot]:
    """按日期升序返回某个仓库的快照序列。"""
    return session.scalars(
        select(RepoSnapshot)
        .where(
            RepoSnapshot.repository_id == repository_id,
            RepoSnapshot.snapshot_date >= start_date,
            RepoSnapshot.snapshot_date <= end_date,
        )
        .order_by(RepoSnapshot.snapshot_date)
    ).all()
