"""守护「研究运行结束 → 报告」这条边的用例。

日常链路原本止步于 `report_drafts`：`reports/cli.py --action generate` 是唯一能组装报告的
途径，而没有任何调度去跑它。于是一个无人值守的日子只产出一份草稿 —— 没有报告、没有可发布
的内容、也没有邮件，而每个阶段都汇报 `succeeded`。
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
    """证明 `finish_run_report` 会组装、校验报告，并把它交给发布策略。"""
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
    # 最终落到哪个状态由策略决定；这里只在乎它靠自己离开了 `draft`。
    assert report.status in {"scheduled", "published", "pending_review", "approved"}


def test_a_run_without_a_draft_reports_the_composition_failure(
    session: Session,
    workspace: Workspace,
) -> None:
    """证明没有候选就没有草稿、没有报告，且调用方拿到的是错误码而非异常。

    Celery 任务在这里绝不能抛错：抛错会触发重试，而重试会把模型预算花在一次证据基础
    本就是空的运行上。
    """
    # 一个没有候选排名的主题：运行在起草之前就停了，因此没有东西可组装。
    # `agent_max_candidates` 表达不了这种情形 —— 它的下限是 1。
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
