"""Tests for the two ledger cards that answer "where did my run go?".

`/ui/runs` used to render only Agent runs and backfills, so the pipeline stages that actually do
the daily work — collect, snapshot, rank, contents, and the per-topic publish/push — were invisible
in the interface. `/ui/repositories` showed its rank/score/selected columns only for a topic filter
and said so in prose that undercounted them, which read as "the columns are missing".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from api_support import Actor, build_client, create_actor, web_login
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from support import NOW, Workspace

from open_source_intelligence.pipeline.runs import (
    finish_pipeline_run,
    start_pipeline_run,
)


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def editor(session: Session) -> Actor:
    """An editor on the team data scope; pipeline runs are `METRICS_ROLES`."""
    return create_actor(session, username="editor", role="EDITOR", data_scope="team")


def _finished_run(session: Session, *, run_type: str, status: str) -> int:
    """Write one settled pipeline run and return its id."""
    run = start_pipeline_run(session, run_type=run_type, topic_id=None, attempt=1)
    finish_pipeline_run(
        session,
        run,
        status=status,
        metrics_json={"stage": run_type},
        error_code=None if status == "succeeded" else "COLLECTOR_FAILED",
        error_message=None if status == "succeeded" else "upstream said no",
        finished_at=NOW + timedelta(seconds=42),
    )
    return int(run.id)


def test_runs_page_lists_pipeline_stages(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """Every stage of the chain is on the page, with its own filters."""
    successful = _finished_run(session, run_type="collect", status="succeeded")
    failed = _finished_run(session, run_type="rank", status="failed")
    assert web_login(client, editor).status_code == 303

    response = client.get("/ui/runs")

    assert response.status_code == 200
    assert "流水线运行" in response.text
    # 类型与状态的取值译成中文，但表格里那一列仍然是可筛选的码。
    assert "采集" in response.text
    assert "排名" in response.text
    assert f'<td class="mono">{successful}</td>' in response.text
    assert f'<td class="mono">{failed}</td>' in response.text
    assert "COLLECTOR_FAILED" in response.text
    assert "耗时" in response.text


def test_runs_page_filters_pipeline_stages_without_losing_the_agent_filter(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """The two filter bars on one page each keep the other's choice."""
    collect_run = _finished_run(session, run_type="collect", status="succeeded")
    rank_run = _finished_run(session, run_type="rank", status="failed")
    assert web_login(client, editor).status_code == 303

    only_failed = client.get("/ui/runs?run_type=rank&run_status=failed")

    assert only_failed.status_code == 200
    assert f'<td class="mono">{rank_run}</td>' in only_failed.text
    assert f'<td class="mono">{collect_run}</td>' not in only_failed.text
    # 另一个筛选条把当前值带在隐藏域里，切一次不会把它清空。
    assert 'name="status"' in only_failed.text
    assert 'name="run_type" value="rank"' in only_failed.text

    # 词汇表外的取值被忽略而不是 500：读页面不该因为一个手改的 URL 炸掉。
    ignored = client.get("/ui/runs?run_type=not-a-stage")
    assert ignored.status_code == 200
    assert f'<td class="mono">{collect_run}</td>' in ignored.text


def test_runs_page_explains_that_needs_review_is_terminal(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """`needs_review` is a verdict on the run, and the page says where the human conclusion goes."""
    assert web_login(client, editor).status_code == 303

    response = client.get("/ui/runs")

    assert response.status_code == 200
    assert "需人工复核" in response.text
    # 没有「通过」这个动作，但有一条通道：结论记在运行上。
    assert "没有「通过」这个动作" in response.text
    assert "复核结论" in response.text
    # 旧标签承诺了一个不存在的动作，不能回到页面上。
    assert "待人工确认" not in response.text
    # 复核筛选是这一列的可达路径，不能被拼错成另一个参数名。
    assert 'name="review"' in response.text


def test_repositories_page_says_why_the_rank_columns_are_absent(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """No topic filter: the three topic-derived columns are gone, and the page says so."""
    assert web_login(client, editor).status_code == 303

    unfiltered = client.get("/ui/repositories")

    assert unfiltered.status_code == 200
    assert "候选排名 / 得分 / 入选" in unfiltered.text
    assert '<th class="num">候选排名</th>' not in unfiltered.text
