"""守护分享卡片：按钮只出现在服务层会接受的地方。

这里有意思的失败是「菜单给得出、点下去 403」这一类。卡片决定要不要渲染按钮时，
用的正是服务层拒绝时依据的那两个常量（`SHAREABLE_SENSITIVITY`、
`SHAREABLE_STATUS`），本文件把这份承诺的两侧都查一遍：渲染出了什么，以及表单
真的提交时会发生什么。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from api_support import Actor, build_client, create_actor, csrf_value, web_login
from fastapi.testclient import TestClient
from reports_support import AUTO_WITH_DELAY, SeededReport, build_report
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import NOW, Workspace

from open_source_intelligence.persistence.models.reporting import Report, ReportShareLink


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def editor(session: Session) -> Actor:
    """团队数据范围上的编辑。"""
    return create_actor(session, username="share-ui-editor", role="EDITOR", data_scope="team")


def seed_report(
    session: Session,
    workspace: Workspace,
    *,
    status: str = "published",
    sensitivity: str = "PUBLIC",
) -> Report:
    """按指定的发布状态与敏感级别写入一份报告。"""
    case: SeededReport = build_report(session, workspace, publish_mode=AUTO_WITH_DELAY)
    report = Report(
        agent_run_id=case.agent_run_id,
        topic_id=case.topic.id,
        title="界面分享用周报",
        current_version=1,
        content_markdown="# 界面分享用周报\n\n正文。\n",
        status=status,
        visibility="INTERNAL",
        sensitivity=sensitivity,
        publish_mode="AUTO_WITH_DELAY",
        generated_at=NOW,
        published_at=NOW if status == "published" else None,
    )
    session.add(report)
    session.commit()
    return report


def test_the_button_is_offered_for_a_public_published_report(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """服务层会接受的地方，卡片就把这个动作给出来。"""
    report = seed_report(session, workspace)
    web_login(client, editor)

    response = client.get(f"/ui/reports/{report.id}")

    assert response.status_code == 200
    assert "生成分享链接" in response.text


@pytest.mark.parametrize(
    ("status", "sensitivity"),
    [("published", "INTERNAL"), ("published", "CONFIDENTIAL"), ("draft", "PUBLIC")],
)
def test_a_disallowed_report_explains_itself_instead_of_hiding(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    editor: Actor,
    status: str,
    sensitivity: str,
) -> None:
    """按钮不见了却不给解释，读起来就像页面坏了。

    拒绝的文字必须点出真正的原因，因为两种原因对应两种不同的修法：改敏感
    级别，或者先把报告发布出去。
    """
    report = seed_report(session, workspace, status=status, sensitivity=sensitivity)
    web_login(client, editor)

    response = client.get(f"/ui/reports/{report.id}")

    assert response.status_code == 200
    assert "当前不能生成分享链接" in response.text
    assert "生成分享链接</button>" not in response.text


def test_posting_the_form_renders_the_link_once(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """响应里带着明文令牌，数据库里则没有。"""
    report = seed_report(session, workspace)
    web_login(client, editor)

    response = client.post(
        f"/ui/reports/{report.id}/share",
        data={"csrf_token": csrf_value(client)},
    )

    assert response.status_code == 200
    assert "/share/" in response.text
    row = session.scalar(select(ReportShareLink).where(ReportShareLink.report_id == report.id))
    assert row is not None
    # 渲染出的页面里有令牌，而那一行里不能有。
    token = response.text.split("/share/")[1].split('"')[0]
    assert row.token_hash != token
    assert token not in str(row.__dict__)


def test_posting_without_csrf_creates_nothing(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """写入路径与其他所有动作一样，保留同一道 CSRF 守卫。"""
    report = seed_report(session, workspace)
    web_login(client, editor)

    response = client.post(f"/ui/reports/{report.id}/share", data={"csrf_token": "wrong"})

    assert response.status_code == 403
    assert session.scalars(select(ReportShareLink)).all() == []


def test_revoking_from_the_panel_stops_the_public_page(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """撤回就是整个安全阀，所以端到端测：卡片 → 公开页 → 404。"""
    report = seed_report(session, workspace)
    web_login(client, editor)
    created = client.post(
        f"/ui/reports/{report.id}/share",
        data={"csrf_token": csrf_value(client)},
    )
    token = created.text.split("/share/")[1].split('"')[0]

    assert client.get(f"/share/{token}").status_code == 200

    row = session.scalar(select(ReportShareLink).where(ReportShareLink.report_id == report.id))
    assert row is not None
    revoked = client.post(
        f"/ui/reports/{report.id}/share/{row.id}/revoke",
        data={"csrf_token": csrf_value(client)},
    )

    assert revoked.status_code == 200
    assert "链接已撤回" in revoked.text
    assert client.get(f"/share/{token}").status_code == 404
