"""守护那两张回答「我这次运行去哪了」的账本卡片。

`/ui/runs` 曾经只渲染 Agent 运行和回填，于是真正干日常活的那几段流水线 —— 采集、
快照、排名、内容，以及按主题的发布与推送 —— 在界面上完全看不见。
`/ui/repositories` 只在按主题筛选时才显示 rank / score / selected 三列，
而且用一段把数量说少的文字来解释，读起来就像「这几列不见了」。
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
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def editor(session: Session) -> Actor:
    """团队数据范围上的编辑；流水线运行属于 `METRICS_ROLES`。"""
    return create_actor(session, username="editor", role="EDITOR", data_scope="team")


def _finished_run(session: Session, *, run_type: str, status: str) -> int:
    """写一条已结束的流水线运行，并返回它的 id。"""
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
    """链路上的每一段都在页面上，并且各自带着自己的筛选条件。"""
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
    """同一页上的两条筛选条，各自保留对方的选择。"""
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
    """`needs_review` 是对这次运行的判定，页面要说清人的结论该记到哪里。"""
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
    """不带主题筛选时：三列主题派生数据都不出现，页面也要说明原因。"""
    assert web_login(client, editor).status_code == 303

    unfiltered = client.get("/ui/repositories")

    assert unfiltered.status_code == 200
    assert "候选排名 / 得分 / 入选" in unfiltered.text
    assert '<th class="num">候选排名</th>' not in unfiltered.text


def test_an_empty_status_parameter_does_not_empty_the_agent_ledger(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """筛选流水线卡片，不能顺手把旁边那张 Agent 卡片清空。

    两条筛选条同处一页，所以各自把对方的当前值放进隐藏域。当对方的取值是「不筛」
    时，那个隐藏域会提交 `status=` —— 一个空字符串，它不是 `None`，而
    `AgentRun.status == ""` 什么也匹配不到。于是卡片显示「还没有 Agent 运行」，
    而库里明明有 51 条：筛选条抹掉了它的邻居，空状态还把锅甩给了数据。
    """
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    run.status = "needs_review"
    run.started_at = NOW
    run.finished_at = NOW
    session.flush()
    assert web_login(client, editor).status_code == 303

    response = client.get("/ui/runs?status=&run_type=collect&run_status=succeeded")

    assert response.status_code == 200
    assert "还没有 Agent 运行" not in response.text
    assert f'href="/ui/runs/{run.id}"' in response.text
