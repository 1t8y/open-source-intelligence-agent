"""守护列表页筛选：GET 表单的「全部」是空串，不能被当成真筛选值。

两份筛选表单一提交就会把所有字段都带进 query string，选「全部」的字段于是变成
`status=` / `actor_id=` 这样的空串。查询层用 `is not None` 判定，空串会被当成真值，
过滤条件变成 `column == ""`，什么都不匹配；`actor_id` 更直接，pydantic 在解析
`int | None` 时遇到空串就返回 422，页面整个空白。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from api_support import Actor, build_client, create_actor, web_login
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """审计页只对管理员开放，所以筛选探针统一用管理员。"""
    return create_actor(session, username="filter-admin", role="ADMIN", data_scope="team")


def _seed_reports(session: Session, workspace: Workspace) -> None:
    """放一份草稿和一份已发布报告，用来区分「筛了」和「没筛」。"""
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    workspace.add_report(topic_id=topic.id, agent_run_id=run.id, title="DRAFT-ONE", status="draft")
    workspace.add_report(
        topic_id=topic.id, agent_run_id=run.id, title="PUBLISHED-ONE", status="published"
    )
    session.flush()


def test_reports_all_options_empty_still_lists_every_report(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """两个筛选项都选「全部」时应当等于未筛选，而不是空页。"""
    _seed_reports(session, workspace)
    assert web_login(client, admin).status_code == 303

    response = client.get("/ui/reports?status=&visibility=")

    assert response.status_code == 200
    assert "DRAFT-ONE" in response.text
    assert "PUBLISHED-ONE" in response.text


def test_reports_status_filter_ignores_the_other_empty_option(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """选了状态、可见性留「全部」时，只应按状态筛。"""
    _seed_reports(session, workspace)
    assert web_login(client, admin).status_code == 303

    response = client.get("/ui/reports?status=draft&visibility=")

    assert response.status_code == 200
    assert "DRAFT-ONE" in response.text
    assert "PUBLISHED-ONE" not in response.text


def test_reports_unknown_status_is_ignored_not_applied(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """URL 里手打一个词表外的状态不该把只读页面变成空页。"""
    _seed_reports(session, workspace)
    assert web_login(client, admin).status_code == 303

    response = client.get("/ui/reports?status=not-a-status")

    assert response.status_code == 200
    assert "DRAFT-ONE" in response.text
    assert "PUBLISHED-ONE" in response.text


def test_audit_empty_actor_id_does_not_break_the_page(admin: Actor, client: TestClient) -> None:
    """筛选表单把空的 actor_id 提交成 `actor_id=`，不应返回 422 空白页。"""
    assert web_login(client, admin).status_code == 303

    response = client.get("/ui/audit?actor_id=&action=&resource_type=&result=")

    assert response.status_code == 200


def test_audit_non_numeric_actor_id_is_ignored(admin: Actor, client: TestClient) -> None:
    """URL 里手打的非法 actor_id 应被忽略，而不是让页面崩掉。"""
    assert web_login(client, admin).status_code == 303

    response = client.get("/ui/audit?actor_id=not-a-number")

    assert response.status_code == 200
