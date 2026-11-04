"""守护「运行未产出报告就停下时，人给出的结论」的用例。

这个状态本身是终态 —— 下游没有任何环节能把 `needs_review` 的运行变成报告 —— 所以一次
复核能诚实做的事只有一件：说明这个解释足以结案，还是需要再试一次。下面既钉住这一点，
也钉住它*不能*做的事：改动状态、覆盖更早的结论、接受一次根本没有东西可复核的运行。
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
    """构造一次真正需要人介入的运行。"""
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
    """证明复核之后运行仍然没有报告，所以给它换个标签就是撒谎。"""
    run = _parked_run(session, workspace)

    record_run_review(session, run_id=run.id, verdict="needs_rerun", now=NOW)

    reloaded = session.get(AgentRun, run.id)
    assert reloaded is not None
    assert reloaded.status == "needs_review"


def test_a_second_review_appends_rather_than_overwrites(
    session: Session, workspace: Workspace
) -> None:
    """证明改主意只追加一行；历史仍保留更早的判断。"""
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
    """证明已完成的运行早就产出了报告，那个决定属于报告，不属于这里。"""
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
    """证明台账会带着空页调用它；它不能发出带 `IN ()` 的查询。"""
    assert latest_run_reviews(session, run_ids=[]) == {}
