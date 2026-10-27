"""Tests for the human conclusion recorded on a run that stopped without a report.

The state itself is terminal — nothing downstream turns a `needs_review` run into a report — so the
only thing a review can honestly do is say whether the explanation closes the case or the work
needs another attempt. These tests pin both that and the things it must *not* do: move the status,
overwrite an earlier conclusion, or accept a run that has nothing to review.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from support import NOW, Workspace

from open_source_intelligence.agent.errors import AgentRunReviewError
from open_source_intelligence.agent.reviews import (
    latest_run_reviews,
    list_run_reviews,
    record_run_review,
)
from open_source_intelligence.persistence.models.agent import AgentRun, AgentRunReview
from open_source_intelligence.persistence.models.audit import AuditLog


def _parked_run(session: Session, workspace: Workspace) -> AgentRun:
    """Create one run in the state that actually asks for a human."""
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    run.status = "needs_review"
    run.started_at = NOW
    run.finished_at = NOW
    run.error_code = "AGENT_EVIDENCE_INCOMPLETE"
    session.flush()
    return run


def test_a_review_is_appended_and_audited(session: Session, workspace: Workspace) -> None:
    run = _parked_run(session, workspace)

    review = record_run_review(
        session,
        run_id=run.id,
        verdict="acknowledged",
        reviewer_id=1,
        comment="窗口太短，候选只有一个，重跑也没用",
        now=NOW,
    )

    assert review.agent_run_id == run.id
    assert review.verdict == "acknowledged"
    assert review.comment == "窗口太短，候选只有一个，重跑也没用"
    events = session.scalars(select(AuditLog).where(AuditLog.action == "agent.review")).all()
    assert [event.result for event in events] == ["success"]
    assert events[0].resource_id == str(run.id)
    assert events[0].metadata_json["verdict"] == "acknowledged"
    assert events[0].metadata_json["error_code"] == "AGENT_EVIDENCE_INCOMPLETE"


def test_a_review_does_not_move_the_run(session: Session, workspace: Workspace) -> None:
    """The run still has no report afterwards, so relabelling it would be a lie."""
    run = _parked_run(session, workspace)

    record_run_review(session, run_id=run.id, verdict="needs_rerun", now=NOW)

    reloaded = session.get(AgentRun, run.id)
    assert reloaded is not None
    assert reloaded.status == "needs_review"


def test_a_second_review_appends_rather_than_overwrites(
    session: Session, workspace: Workspace
) -> None:
    """Changing your mind adds a line; the trail keeps the earlier judgement."""
    run = _parked_run(session, workspace)
    record_run_review(session, run_id=run.id, verdict="acknowledged", now=NOW)
    record_run_review(
        session,
        run_id=run.id,
        verdict="needs_rerun",
        comment="补了 README 缓存，值得再跑一次",
        now=NOW,
    )

    reviews = list_run_reviews(session, run_id=run.id)
    assert [review.verdict for review in reviews] == ["acknowledged", "needs_rerun"]
    latest = latest_run_reviews(session, run_ids=[run.id])
    assert latest[run.id].verdict == "needs_rerun"


def test_a_completed_run_is_not_reviewed_here(session: Session, workspace: Workspace) -> None:
    """A finished run already produced a report; the decision belongs to that report."""
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    run.status = "completed"
    session.flush()

    with pytest.raises(AgentRunReviewError) as caught:
        record_run_review(session, run_id=run.id, verdict="acknowledged", now=NOW)

    assert caught.value.reason == "not_reviewable"
    assert session.scalars(select(AgentRunReview)).all() == []


def test_an_unknown_run_is_refused(session: Session, workspace: Workspace) -> None:
    with pytest.raises(AgentRunReviewError) as caught:
        record_run_review(session, run_id=987_654, verdict="acknowledged", now=NOW)

    assert caught.value.reason == "not_found"


def test_an_unknown_verdict_is_refused(session: Session, workspace: Workspace) -> None:
    run = _parked_run(session, workspace)

    with pytest.raises(AgentRunReviewError) as caught:
        record_run_review(session, run_id=run.id, verdict="approve", now=NOW)

    assert caught.value.reason == "invalid_verdict"
    assert session.scalars(select(AgentRunReview)).all() == []


def test_latest_reviews_for_no_runs_is_one_empty_read(session: Session) -> None:
    """The ledger calls this with an empty page; it must not issue a query with `IN ()`."""
    assert latest_run_reviews(session, run_ids=[]) == {}
