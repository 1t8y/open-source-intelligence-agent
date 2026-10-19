"""Shared seeding helpers for the observability tests.

It lives at the top level of `tests/` because the API and web suites both need it, and the
per-directory test folders carry no `__init__.py`.

The ledger it builds is deliberately small but not trivial: one block of rows inside every
window the dashboard offers, one block older than all of them, a mix of outcomes, and one tool
call whose step link has degraded to null. A fixture that only ever contains clean successes
would let the failure-rate and trace assertions pass without measuring anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session
from support import Workspace

from open_source_intelligence.persistence.models.agent import AgentRun, AgentStep, ToolCall
from open_source_intelligence.persistence.models.intelligence import PipelineRun
from open_source_intelligence.persistence.models.reporting import Report

#: The instant every observability test measures against, anchored to *yesterday noon*.
#:
#: It has to move with the clock: the observation window is rolling (`now - days`), so a fixed
#: anchor makes the ledger fall out of every window on a calendar boundary. That happened for real
#: - rows seeded at `2026-10-12T12:00Z` sat inside a seven-day window on the 18th and outside it on
#: the 19th, and four metrics/dashboard tests went red with no code change behind them.
#:
#: One day back rather than "now" keeps the newest band (`ANCHOR - 3h`) comfortably inside the
#: narrowest window no matter what hour the suite runs at, and it stays clear of the newest edge so
#: `started_at <= window_end` never depends on which second the process started.
ANCHOR = (datetime.now(UTC) - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)

#: Offsets from the anchor. Three bands, so "the window filters" is a claim with something to
#: filter: inside, outside the 7-day window, and outside every window the dashboard offers.
RECENT = timedelta(hours=3)
PRIOR = timedelta(days=20)
STALE = timedelta(days=40)

DENIED_TOOL = "get_repository_content"
DENIED_ERROR = "TOOL_NOT_ALLOWED"
FAILURE_ERROR = "AGENT_TOOL_BUDGET_EXCEEDED"
UNLINKED_ERROR = "TOOL_TIMEOUT"

#: A credential-shaped value that must never reach a client through either egress path.
LEAKED_SECRET = "sk-Abcdefghij0123456789"


@dataclass(frozen=True, slots=True)
class SeededLedger:
    """The ids the observability tests assert against."""

    topic_id: int
    failing_run_id: int
    completed_run_id: int
    report_generated_ids: tuple[int, ...]


def _agent_run(
    session: Session,
    workspace: Workspace,
    topic_id: int,
    *,
    status: str,
    started_at: datetime,
    duration_ms: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost: str = "0",
    publish_mode: str | None = None,
    error_code: str | None = None,
) -> AgentRun:
    """Create one Agent run whose pipeline row carries the same duration."""
    run = workspace.add_agent_run(topic_id)
    pipeline = session.get(PipelineRun, run.pipeline_run_id)
    assert pipeline is not None
    pipeline.status = "succeeded" if status != "failed" else "failed"
    pipeline.started_at = started_at
    pipeline.finished_at = started_at + timedelta(milliseconds=duration_ms)
    run.status = status
    run.started_at = started_at
    run.finished_at = started_at + timedelta(milliseconds=duration_ms)
    run.prompt_tokens = prompt_tokens
    run.completion_tokens = completion_tokens
    run.estimated_cost = Decimal(cost)
    run.publish_mode = publish_mode
    run.error_code = error_code
    session.flush()
    return run


def seed_ledger(session: Session, workspace: Workspace) -> SeededLedger:
    """Seed the topics, runs, traces, and reports the observability tests read."""
    topic = workspace.add_topic()

    # Inside every window: one clean run, one parked for a human, one that failed.
    completed = _agent_run(
        session,
        workspace,
        topic.id,
        status="completed",
        started_at=ANCHOR - RECENT,
        duration_ms=40_000,
        prompt_tokens=1_200,
        completion_tokens=300,
        cost="0.012300",
        publish_mode="AUTO_WITH_DELAY",
    )
    parked = _agent_run(
        session,
        workspace,
        topic.id,
        status="needs_review",
        started_at=ANCHOR - RECENT - timedelta(hours=1),
        duration_ms=60_000,
        prompt_tokens=900,
        completion_tokens=200,
        cost="0.008000",
        publish_mode="MANUAL",
    )
    failing = _agent_run(
        session,
        workspace,
        topic.id,
        status="failed",
        started_at=ANCHOR - RECENT - timedelta(hours=2),
        duration_ms=80_000,
        prompt_tokens=400,
        completion_tokens=50,
        error_code=FAILURE_ERROR,
    )

    # Outside the 7-day window but inside the 30-day one.
    _agent_run(
        session,
        workspace,
        topic.id,
        status="completed",
        started_at=ANCHOR - PRIOR,
        duration_ms=20_000,
    )
    # Outside every window the dashboard offers.
    _agent_run(
        session,
        workspace,
        topic.id,
        status="failed",
        started_at=ANCHOR - STALE,
        duration_ms=5_000,
        error_code="AGENT_STALE_TIMEOUT",
    )

    # Two more pipeline rows of other types, so the run-type breakdown is not a single key.
    for index, (run_type, status, duration_ms) in enumerate(
        (("collect", "succeeded", 10_000), ("rank", "failed", 30_000))
    ):
        session.add(
            PipelineRun(
                topic_id=topic.id,
                run_type=run_type,
                status=status,
                attempt=1,
                started_at=ANCHOR - RECENT - timedelta(hours=index + 3),
                finished_at=ANCHOR
                - RECENT
                - timedelta(hours=index + 3)
                + timedelta(milliseconds=duration_ms),
            )
        )

    # Reports: one published, one parked for review, one older than the 7-day window.
    published = workspace.add_report(
        topic_id=topic.id,
        agent_run_id=completed.id,
        title="T15 published",
        status="published",
        generated_at=ANCHOR - RECENT,
    )
    blocked = workspace.add_report(
        topic_id=topic.id,
        agent_run_id=parked.id,
        title="T15 pending review",
        status="pending_review",
        generated_at=ANCHOR - RECENT - timedelta(hours=1),
    )
    old = workspace.add_report(
        topic_id=topic.id,
        agent_run_id=failing.id,
        title="T15 stale",
        status="failed",
        generated_at=ANCHOR - STALE,
    )

    # The failing run's trajectory, including one step and one unlinked tool call.
    plan = AgentStep(
        agent_run_id=failing.id,
        step_index=1,
        step_type="plan",
        status="succeeded",
        summary="规划本轮研究问题",
        latency_ms=5_000,
        created_at=ANCHOR - RECENT - timedelta(hours=2),
    )
    denied_step = AgentStep(
        agent_run_id=failing.id,
        step_index=2,
        step_type="tool",
        status="failed",
        summary="调用 get_repository_content 被拒绝",
        latency_ms=800,
        created_at=ANCHOR - RECENT - timedelta(hours=2),
    )
    session.add_all([plan, denied_step])
    session.flush()
    session.add_all(
        [
            ToolCall(
                agent_run_id=failing.id,
                agent_step_id=plan.id,
                tool_name="query_repo_metrics",
                arguments_json={"topic_id": topic.id},
                result_summary="返回 1 个仓库的指标序列",
                status="success",
                latency_ms=300,
                created_at=ANCHOR - RECENT - timedelta(hours=2),
            ),
            ToolCall(
                agent_run_id=failing.id,
                agent_step_id=denied_step.id,
                tool_name=DENIED_TOOL,
                arguments_json={"topic_id": topic.id},
                result_summary=f"读取被拒绝，调试令牌 {LEAKED_SECRET}",
                status="denied",
                error_code=DENIED_ERROR,
                latency_ms=800,
                created_at=ANCHOR - RECENT - timedelta(hours=2),
            ),
            ToolCall(
                agent_run_id=failing.id,
                agent_step_id=None,
                tool_name="search_prior_reports",
                arguments_json={"topic_id": topic.id},
                result_summary="上游超时",
                status="failed",
                error_code=UNLINKED_ERROR,
                latency_ms=200,
                created_at=ANCHOR - RECENT - timedelta(hours=2),
            ),
        ]
    )
    session.commit()
    return SeededLedger(
        topic_id=topic.id,
        failing_run_id=failing.id,
        completed_run_id=completed.id,
        report_generated_ids=(published.id, blocked.id, old.id),
    )


def run_count(session: Session) -> int:
    """Return how many Agent runs the ledger currently holds."""
    return len(session.scalars(select(AgentRun)).all())


def report_titles(session: Session) -> list[str]:
    """Return every report title, sorted, so a test can prove nothing was written."""
    return sorted(session.scalars(select(Report.title)).all())
