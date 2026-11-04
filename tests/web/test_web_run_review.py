"""守护运行页上的复核表单，以及读取它的那一列账本。

停在 `needs_review` 的运行没有报告，所以没有什么可批准的 —— 但在它出现之前，也没
有地方记下「有人看过了」，而从「采集与运行」点进一次运行，会掉进一个把侧边栏高亮
在「运维看板」上、只提供一条回面板的路的页面。两件事都在这里钉住。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from api_support import Actor, build_client, create_actor, csrf_value, web_login
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import NOW, Workspace

from open_source_intelligence.persistence.models.agent import AgentRun, AgentRunReview
from open_source_intelligence.persistence.models.audit import AuditLog


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def editor(session: Session) -> Actor:
    """编辑：运行账本与这套复核都归 `METRICS_ROLES` 管。"""
    return create_actor(session, username="editor", role="EDITOR", data_scope="team")


def _run(
    session: Session, workspace: Workspace, *, status: str, name: str = "AI Agent"
) -> AgentRun:
    """按给定的终态创建一次 Agent 运行。

    `topics.name` 是唯一的，所以一个需要两次运行的用例要传两个名字。
    """
    topic = workspace.add_topic(name=name)
    run = workspace.add_agent_run(topic.id)
    run.status = status
    run.started_at = NOW
    run.finished_at = NOW
    if status == "needs_review":
        run.error_code = "AGENT_EVIDENCE_INCOMPLETE"
    session.flush()
    return run


def _review(client: TestClient, run_id: int, **fields: str):
    """用一枚新的 CSRF 令牌提交一次复核，并且不跟随重定向。"""
    payload = {"csrf_token": csrf_value(client), "verdict": "acknowledged", "comment": ""}
    payload.update(fields)
    return client.post(f"/ui/runs/{run_id}/review", data=payload, follow_redirects=False)


def test_the_run_page_belongs_to_the_ledger_that_lists_it(
    session: Session, workspace: Workspace, editor: Actor, client: TestClient
) -> None:
    """打开一次运行，高亮「采集与运行」而不是「运维看板」，并且能回到账本。"""
    run = _run(session, workspace, status="needs_review")
    assert web_login(client, editor).status_code == 303

    response = client.get(f"/ui/runs/{run.id}")

    assert response.status_code == 200
    text = response.text
    assert f"运行 #{run.id}" in text
    assert 'href="/ui/runs" aria-current="page"' in text
    assert 'href="/ui/dashboard" aria-current="page"' not in text
    assert "← 返回采集与运行" in text


def test_the_run_page_says_why_there_is_nothing_to_approve(
    session: Session, workspace: Workspace, editor: Actor, client: TestClient
) -> None:
    """错误码被译成人话，并且把三条出路逐一写清。"""
    run = _run(session, workspace, status="needs_review")
    assert web_login(client, editor).status_code == 303

    text = client.get(f"/ui/runs/{run.id}").text

    assert "没有产出报告" in text
    assert "证据不完整" in text
    assert "立即研究" in text
    assert f'action="/ui/runs/{run.id}/review"' in text
    assert "记录复核结论" in text


def test_recording_a_review_lands_on_the_page_and_in_the_audit(
    session: Session, workspace: Workspace, editor: Actor, client: TestClient
) -> None:
    run = _run(session, workspace, status="needs_review")
    assert web_login(client, editor).status_code == 303

    response = _review(client, run.id, comment="窗口太短，先不动")

    assert response.status_code == 303
    assert response.headers["location"] == f"/ui/runs/{run.id}?reviewed=1"
    page = client.get(f"/ui/runs/{run.id}?reviewed=1")
    assert "已记录复核结论" in page.text
    assert "已确认原因" in page.text
    assert "窗口太短，先不动" in page.text
    session.rollback()
    stored = session.scalars(select(AgentRunReview).where(AgentRunReview.agent_run_id == run.id))
    assert [row.verdict for row in stored.all()] == ["acknowledged"]
    audits = session.scalars(select(AuditLog).where(AuditLog.action == "agent.review")).all()
    assert [(event.result, event.resource_id) for event in audits] == [("success", str(run.id))]


def test_reviewing_a_completed_run_is_refused_in_the_page_s_own_words(
    session: Session, workspace: Workspace, editor: Actor, client: TestClient
) -> None:
    """已完成的运行，结论归属于它产出的报告；拒绝要说清楚，而不是给一个 500。"""
    run = _run(session, workspace, status="completed")
    assert web_login(client, editor).status_code == 303

    response = _review(client, run.id)

    assert response.status_code == 400
    assert "结论在它产出的那份报告上" in response.text
    session.rollback()
    assert session.scalars(select(AgentRunReview)).all() == []


def test_a_completed_run_offers_no_form_at_all(
    session: Session, workspace: Workspace, editor: Actor, client: TestClient
) -> None:
    run = _run(session, workspace, status="completed")
    assert web_login(client, editor).status_code == 303

    text = client.get(f"/ui/runs/{run.id}").text

    assert f'action="/ui/runs/{run.id}/review"' not in text
    assert "只有「需人工复核」的运行记录复核结论" in text


def test_the_ledger_shows_the_last_verdict_and_filters_by_it(
    session: Session, workspace: Workspace, editor: Actor, client: TestClient
) -> None:
    reviewed = _run(session, workspace, status="needs_review", name="Review read")
    unread = _run(session, workspace, status="needs_review", name="Review unread")
    assert web_login(client, editor).status_code == 303
    assert _review(client, reviewed.id).status_code == 303

    everything = client.get("/ui/runs").text
    assert "待复核" in everything  # 还没人读过的那个
    assert "已确认原因" in everything

    open_only = client.get("/ui/runs?review=open")
    assert open_only.status_code == 200
    assert f'href="/ui/runs/{unread.id}"' in open_only.text
    assert f'href="/ui/runs/{reviewed.id}"' not in open_only.text

    done_only = client.get("/ui/runs?review=done")
    assert f'href="/ui/runs/{reviewed.id}"' in done_only.text
    assert f'href="/ui/runs/{unread.id}"' not in done_only.text

    # 词汇表外的取值被忽略而不是往下传，与这一页的其他筛选器一致。
    typo = client.get("/ui/runs?review=nonsense")
    assert typo.status_code == 200
    assert f'href="/ui/runs/{reviewed.id}"' in typo.text


def test_the_old_dashboard_run_link_still_resolves(
    session: Session, workspace: Workspace, editor: Actor, client: TestClient
) -> None:
    """书签和外链活得比一次搬家更久；旧路径用一次重定向作答。"""
    run = _run(session, workspace, status="needs_review")
    assert web_login(client, editor).status_code == 303

    response = client.get(f"/ui/dashboard/runs/{run.id}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/ui/runs/{run.id}"


def test_a_step_shows_the_tool_calls_that_belong_to_it(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """轨迹按 `agent_step_id` 归组；没挂上的调用不属于任何一步。

    过去每次调用都写成空的 `agent_step_id`，于是每一步都声称自己没有工具调用，
    而它自己的摘要却写着 `executed 5 tool call(s)`，这次运行的调用全都落进一张
    甩锅给已删除行的卡片。现在图会写下关联（`StepRecorder.link_tool_calls`），
    这里钉的是页面拿它做了什么。
    """
    run = _run(session, workspace, status="needs_review")
    step = workspace.add_agent_step(run.id, step_type="tool", summary="executed 1 tool call(s)")
    call = workspace.add_tool_call(run.id, step.id, tool_name="get_repository_profile")
    assert call.agent_step_id == step.id
    assert web_login(client, editor).status_code == 303

    page = client.get(f"/ui/runs/{run.id}")

    assert page.status_code == 200
    # 步骤徽章被译成中文，而那次调用渲染在该步骤之下，而不是落进孤儿卡片。
    assert "调用工具 · 正常" in page.text
    assert "get_repository_profile" in page.text
    assert "未挂到步骤上的工具调用" not in page.text
    assert "该步骤没有工具调用" not in page.text
