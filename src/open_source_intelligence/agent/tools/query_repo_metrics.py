"""`query_repo_metrics` implementation: daily snapshot series for one repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.intelligence import RepoSnapshot

from .access import load_authorized_repository
from .context import ToolContext
from .contracts import MetricPoint, QueryRepoMetricsInput
from .outcome import ToolOutcome


def query_repo_metrics(
    session: Session,
    ctx: ToolContext,
    payload: QueryRepoMetricsInput,
) -> ToolOutcome:
    """Return daily snapshot points inside the requested window."""
    load_authorized_repository(session, ctx, payload.repo_id)
    rows = session.scalars(
        select(RepoSnapshot)
        .where(
            RepoSnapshot.repository_id == payload.repo_id,
            RepoSnapshot.snapshot_date >= payload.start.date(),
            RepoSnapshot.snapshot_date <= payload.end.date(),
        )
        .order_by(RepoSnapshot.snapshot_date)
    ).all()
    points = [
        MetricPoint(
            snapshot_date=row.snapshot_date,
            stars=row.stars,
            forks=row.forks,
            open_issues=row.open_issues,
            stars_delta=row.stars_delta,
            forks_delta=row.forks_delta,
        )
        for row in rows
    ]
    return ToolOutcome(data=points, result_count=len(points), truncated=False)
