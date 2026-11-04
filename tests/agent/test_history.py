"""守护 Agent 运行历史查询的用例。"""

from __future__ import annotations

import json
from decimal import Decimal

from agent_support import seed_topic
from sqlalchemy.orm import Session
from support import NOW, Workspace, build_settings

from open_source_intelligence.agent.history import (
    get_agent_run,
    list_agent_runs,
    list_agent_steps,
    list_tool_calls,
    summarize_agent_run,
    summarize_agent_steps,
    summarize_tool_calls,
)
from open_source_intelligence.agent.runtime import run_research_agent


def _execute(session: Session, workspace: Workspace) -> tuple[int, int]:
    seeded = seed_topic(workspace, repo_count=1)
    result = run_research_agent(
        session,
        topic_id=seeded.topic.id,
        settings=build_settings(),
        now=NOW,
    )
    return result.agent_run_id, seeded.topic.id


def test_get_agent_run_returns_the_requested_run(session: Session, workspace: Workspace) -> None:
    run_id, _ = _execute(session, workspace)
    run = get_agent_run(session, run_id)
    assert run is not None
    assert run.id == run_id


def test_list_agent_runs_filters_by_topic_and_status(
    session: Session,
    workspace: Workspace,
) -> None:
    run_id, topic_id = _execute(session, workspace)

    assert [run.id for run in list_agent_runs(session, topic_id=topic_id)] == [run_id]
    assert list_agent_runs(session, topic_id=topic_id + 1000) == []
    assert run_id in [run.id for run in list_agent_runs(session, status="completed")]
    assert list_agent_runs(session, status="failed") == []


def test_run_summary_is_json_safe(session: Session, workspace: Workspace) -> None:
    run_id, _ = _execute(session, workspace)
    run = get_agent_run(session, run_id)
    assert run is not None

    summary = summarize_agent_run(run)

    json.dumps(summary)
    assert summary["status"] == "completed"
    assert summary["error_code"] is None
    assert Decimal(summary["estimated_cost"]) == 0


def test_steps_are_ordered_and_summarized(session: Session, workspace: Workspace) -> None:
    run_id, _ = _execute(session, workspace)
    steps = list_agent_steps(session, run_id)

    assert [step.step_index for step in steps] == list(range(1, len(steps) + 1))

    summaries = summarize_agent_steps(steps)
    assert [item["step_index"] for item in summaries] == list(range(1, len(steps) + 1))
    assert all(item["summary"] for item in summaries)


def test_tool_calls_are_summarized(session: Session, workspace: Workspace) -> None:
    run_id, _ = _execute(session, workspace)
    summaries = summarize_tool_calls(list_tool_calls(session, run_id))

    assert summaries
    assert all(item["status"] == "success" for item in summaries)
    assert {item["tool_name"] for item in summaries} == {
        "get_repository_profile",
        "query_repo_metrics",
        "search_prior_reports",
        "get_repository_content",
        "save_report_section",
    }
