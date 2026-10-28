"""Tests for the edge that turns a finished research run into a report.

The daily chain used to stop at `report_drafts`: `reports/cli.py --action generate` was the only
way to compose a report, and nothing scheduled it. An unattended day therefore produced a draft,
no report, nothing to publish, and no email - while every stage reported `succeeded`.
"""

from __future__ import annotations

from agent_support import seed_topic
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from support import NOW, Workspace, build_settings

from open_source_intelligence.agent.runtime import run_research_agent
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.reports.automation import finish_run_report


def test_a_finished_run_reaches_a_verified_report(
    session: Session,
    workspace: Workspace,
) -> None:
    """`finish_run_report` composes, verifies, and hands the report to the publish policy."""
    seeded = seed_topic(workspace, repo_count=2)
    result = run_research_agent(
        session,
        topic_id=seeded.topic.id,
        settings=build_settings(),
        now=NOW,
    )
    assert result.report_draft_id is not None

    summary = finish_run_report(session, agent_run_id=result.agent_run_id)

    assert summary["stage"] == "publish"
    assert summary["verdict"] == "passed"
    report = session.get(Report, summary["report_id"])
    assert report is not None
    # Where it lands is the policy's call; what matters is that it left `draft` on its own.
    assert report.status in {"scheduled", "published", "pending_review", "approved"}


def test_a_run_without_a_draft_reports_the_composition_failure(
    session: Session,
    workspace: Workspace,
) -> None:
    """No candidates, no draft, no report — and the caller gets a code instead of an exception.

    A Celery task must not raise here: raising triggers a retry, and a retry spends model budget
    on a run whose evidence base was empty to begin with.
    """
    # A topic with no ranked candidates: the run stops before drafting, so there is nothing to
    # compose. `agent_max_candidates` cannot express this - its floor is 1.
    topic = workspace.add_topic()
    result = run_research_agent(
        session,
        topic_id=topic.id,
        settings=build_settings(),
        now=NOW,
    )
    assert result.report_draft_id is None

    summary = finish_run_report(session, agent_run_id=result.agent_run_id)

    assert summary["stage"] == "generate"
    assert summary["report_id"] is None
    assert summary["error_code"]
    assert session.scalar(select(func.count()).select_from(Report)) == 0
