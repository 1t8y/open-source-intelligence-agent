"""Human conclusions about runs that stopped without a report.

A `needs_review` run is terminal. Nothing downstream can approve it, because it produced no draft
to approve; the only honest question a reviewer answers is "is this explanation enough, or does it
need another attempt". This module is where that answer lands.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.errors import AgentRunReviewError
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.agent import (
    AGENT_RUN_REVIEW_VERDICTS,
    AgentRun,
    AgentRunReview,
)
from open_source_intelligence.security.audit import record_audit_event

#: The only run status a review applies to. A `completed` run already produced a report - the
#: human decision belongs to that report, not here; `failed` broke before it had anything to read.
REVIEWABLE_RUN_STATUS = "needs_review"


def record_run_review(
    session: Session,
    *,
    run_id: int,
    verdict: str,
    reviewer_id: int | None = None,
    comment: str | None = None,
    now: datetime | None = None,
) -> AgentRunReview:
    """Append one human conclusion to a run that stopped for review.

    The verdict deliberately does not touch `AgentRun.status`. The run still has no report
    afterwards - relabelling it would claim a draft exists. What changes is that the next reader
    can see this row was already read, and what the reader concluded.
    """
    if verdict not in AGENT_RUN_REVIEW_VERDICTS:
        allowed = ", ".join(AGENT_RUN_REVIEW_VERDICTS)
        raise AgentRunReviewError(
            f"unsupported verdict: {verdict}; expected one of {allowed}",
            reason="invalid_verdict",
        )
    run = session.get(AgentRun, run_id)
    if run is None:
        raise AgentRunReviewError(f"run {run_id} does not exist", reason="not_found")
    if run.status != REVIEWABLE_RUN_STATUS:
        raise AgentRunReviewError(
            f"run {run.id} is {run.status}; only a run in {REVIEWABLE_RUN_STATUS} is reviewed "
            f"here. A completed run is reviewed through its report, not through the run.",
            reason="not_reviewable",
        )

    review = AgentRunReview(
        agent_run_id=run.id,
        reviewer_id=reviewer_id,
        verdict=verdict,
        comment=comment,
        created_at=now or utc_now(),
    )
    session.add(review)
    record_audit_event(
        session,
        action="agent.review",
        resource_type="agent_run",
        resource_id=str(run.id),
        result="success",
        actor_id=reviewer_id,
        metadata_json={
            "verdict": verdict,
            "comment": comment,
            "run_status": run.status,
            "error_code": run.error_code,
        },
    )
    session.commit()
    return review


def list_run_reviews(session: Session, *, run_id: int) -> tuple[AgentRunReview, ...]:
    """Return one run's reviews, oldest first, so the page can show the history."""
    return tuple(
        session.scalars(
            select(AgentRunReview)
            .where(AgentRunReview.agent_run_id == run_id)
            .order_by(AgentRunReview.id)
        ).all()
    )


def latest_run_reviews(
    session: Session,
    *,
    run_ids: Sequence[int],
) -> dict[int, AgentRunReview]:
    """Return the newest review per run for a page of runs, in one read.

    Batched on purpose: the ledger shows 20 rows and asking per row is one query per row. Ties on
    `id` cannot happen (it is the primary key), so "last one wins while scanning ascending" is
    exactly "newest".
    """
    if not run_ids:
        return {}
    rows = session.scalars(
        select(AgentRunReview)
        .where(AgentRunReview.agent_run_id.in_(tuple(run_ids)))
        .order_by(AgentRunReview.id)
    ).all()
    return {row.agent_run_id: row for row in rows}
