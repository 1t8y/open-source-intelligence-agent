"""对「没有产出报告就停下」的运行所下的人工结论。

一次 `needs_review` 运行就是终态。下游没有任何东西能批准它，因为它没有产出可供
批准的草稿；复核者唯一诚实要回答的问题是「这个解释够不够，还是需要再跑一次」。
本模块就是这些答案的落点。
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

#: 复核只适用于这一种运行状态。`completed` 运行已经产出了报告 —— 人工决定属于
#: 那份报告，而不在这里；`failed` 则在有任何可读内容之前就崩了。
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
    """给一次因待复核而停下的运行追加一条人工结论。

    结论刻意不去动 `AgentRun.status`。运行之后依然没有报告 —— 改标签就等于
    宣称已有草稿。变的是：下一个读者能看到这一行已经被读过，以及读的人得出
    了什么结论。
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
    """按从旧到新的顺序返回一次运行的复核记录，页面才能展示历史。"""
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
    """一次读取就为整页运行各取最新一条复核记录。

    特意做成批量：台账一页 20 行，逐行去问就是一行一次查询。`id` 上不可能出现
    并列（它是主键），所以「按升序扫描时最后一条胜出」恰好等于「最新」。
    """
    if not run_ids:
        return {}
    rows = session.scalars(
        select(AgentRunReview)
        .where(AgentRunReview.agent_run_id.in_(tuple(run_ids)))
        .order_by(AgentRunReview.id)
    ).all()
    return {row.agent_run_id: row for row in rows}
