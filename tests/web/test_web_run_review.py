"""Tests for the run page's review form and the ledger column that reads it.

A run that stopped with `needs_review` has no report, so there is nothing to approve — but until
this existed there was also nowhere to record that somebody had looked at it, and opening a run
from 采集与运行 dropped the reader into a page that highlighted 运维看板 in the sidebar and offered
only a way back to the dashboard. Both are pinned here.
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
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def editor(session: Session) -> Actor:
    """An editor: the run ledger and this review sit with `METRICS_ROLES`."""
    return create_actor(session, username="editor", role="EDITOR", data_scope="team")


def _run(
    session: Session, workspace: Workspace, *, status: str, name: str = "AI Agent"
) -> AgentRun:
    """Create one Agent run in the given terminal state.

    `topics.name` is unique, so a test that needs two runs passes two names.
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
    """POST one review with a fresh CSRF token, without following the redirect."""
    payload = {"csrf_token": csrf_value(client), "verdict": "acknowledged", "comment": ""}
    payload.update(fields)
    return client.post(f"/ui/runs/{run_id}/review", data=payload, follow_redirects=False)


def test_the_run_page_belongs_to_the_ledger_that_lists_it(
    session: Session, workspace: Workspace, editor: Actor, client: TestClient
) -> None:
    """Opening a run highlights 采集与运行, not 运维看板, and leads back to the ledger."""
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
    """The error code is translated and the three ways forward are spelled out."""
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
    """A finished run's decision belongs to its report; the refusal says so instead of 500ing."""
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
    assert "待复核" in everything  # the one nobody has read yet
    assert "已确认原因" in everything

    open_only = client.get("/ui/runs?review=open")
    assert open_only.status_code == 200
    assert f'href="/ui/runs/{unread.id}"' in open_only.text
    assert f'href="/ui/runs/{reviewed.id}"' not in open_only.text

    done_only = client.get("/ui/runs?review=done")
    assert f'href="/ui/runs/{reviewed.id}"' in done_only.text
    assert f'href="/ui/runs/{unread.id}"' not in done_only.text

    # An unnamed value is ignored rather than forwarded, like the other filters on this page.
    typo = client.get("/ui/runs?review=nonsense")
    assert typo.status_code == 200
    assert f'href="/ui/runs/{reviewed.id}"' in typo.text


def test_the_old_dashboard_run_link_still_resolves(
    session: Session, workspace: Workspace, editor: Actor, client: TestClient
) -> None:
    """Bookmarks and external links outlive a move; the old path answers with a redirect."""
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
    """The trajectory groups calls by `agent_step_id`; an unlinked call belongs to no step.

    Every call used to be written with a null `agent_step_id`, so each step claimed it had no
    tool calls while its own summary said `executed 5 tool call(s)`, and the run's calls all fell
    through to a card blaming deleted rows. The graph writes the link now
    (`StepRecorder.link_tool_calls`); this pins what the page does with it.
    """
    run = _run(session, workspace, status="needs_review")
    step = workspace.add_agent_step(run.id, step_type="tool", summary="executed 1 tool call(s)")
    call = workspace.add_tool_call(run.id, step.id, tool_name="get_repository_profile")
    assert call.agent_step_id == step.id
    assert web_login(client, editor).status_code == 303

    page = client.get(f"/ui/runs/{run.id}")

    assert page.status_code == 200
    # The step badge is translated, and the call is rendered under that step rather than in the
    # orphan card.
    assert "调用工具 · 正常" in page.text
    assert "get_repository_profile" in page.text
    assert "未挂到步骤上的工具调用" not in page.text
    assert "该步骤没有工具调用" not in page.text
