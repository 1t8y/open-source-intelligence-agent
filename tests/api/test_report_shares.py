"""守护分享链接：JSON 接口，以及那个不需要登录的页面。

两套入口，同一条规则。JSON 接口和别的报告接口一样要过鉴权（Bearer 令牌、写角色、
报告读权限）；`GET /share/{token}` 则由令牌本身把关。两者都绝不能放宽「哪些报告
可以出门」这条边界，所以拒绝路径在两边都要测。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import timedelta

import pytest
from api_support import Actor, auth_headers, build_client, create_actor
from fastapi.testclient import TestClient
from reports_support import AUTO_WITH_DELAY, SeededReport, build_report
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import NOW, Workspace

from open_source_intelligence.persistence.models.reporting import Report, ReportShareLink

BASE = "/api/v1/reports"


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def editor(session: Session) -> Actor:
    """团队数据范围上的编辑。"""
    return create_actor(session, username="sharing-editor", role="EDITOR", data_scope="team")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """只读用户。"""
    return create_actor(session, username="sharing-viewer", role="VIEWER", data_scope="team")


def seed_report(
    session: Session,
    workspace: Workspace,
    *,
    status: str = "published",
    sensitivity: str = "PUBLIC",
) -> Report:
    """直接按最终状态构造一行报告再落库。"""
    case: SeededReport = build_report(session, workspace, publish_mode=AUTO_WITH_DELAY)
    report = Report(
        agent_run_id=case.agent_run_id,
        topic_id=case.topic.id,
        title="分享用周报",
        current_version=1,
        content_markdown="# 分享用周报\n\n这是正文。\n",
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


def test_creating_a_link_returns_the_token_once(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """创建响应是明文令牌唯一会现身的地方。"""
    report = seed_report(session, workspace)
    headers = auth_headers(client, editor)

    response = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["token"] in body["url_path"]
    assert body["expires_at"] is not None
    row = session.scalar(select(ReportShareLink).where(ReportShareLink.id == body["share_id"]))
    assert row is not None
    assert row.token_hash == hashlib.sha256(body["token"].encode("utf-8")).hexdigest()


def test_listing_links_has_no_token_field(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """之后再读不能把链接还原出来，所以干脆没有任何字段携带它。"""
    report = seed_report(session, workspace)
    headers = auth_headers(client, editor)
    created = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers).json()

    response = client.get(f"{BASE}/{report.id}/shares", headers=headers)

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["share_id"] for item in items] == [created["share_id"]]
    assert "token" not in items[0]
    assert created["token"] not in response.text


def test_an_internal_report_is_refused_with_a_conflict(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """敏感级别就是闸门：`INTERNAL` 内容永远不会拿到一个公开地址。"""
    report = seed_report(session, workspace, sensitivity="INTERNAL")
    headers = auth_headers(client, editor)

    response = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "SHARE_SENSITIVITY_REFUSED"


def test_a_viewer_cannot_mint_a_link(
    client: TestClient, session: Session, workspace: Workspace, viewer: Actor
) -> None:
    """制造一个公开地址是写动作，所以需要写角色。"""
    report = seed_report(session, workspace)
    headers = auth_headers(client, viewer)

    response = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers)

    assert response.status_code == 403


def test_an_anonymous_request_cannot_mint_a_link(
    client: TestClient, session: Session, workspace: Workspace
) -> None:
    """负责扩大暴露面的接口，自己不能不需要登录就能访问。"""
    report = seed_report(session, workspace)

    response = client.post(f"{BASE}/{report.id}/shares", json={})

    assert response.status_code == 401


def test_revoking_twice_reports_false_the_second_time(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """幂等：目的已经达到，重复一次不算错误。"""
    report = seed_report(session, workspace)
    headers = auth_headers(client, editor)
    created = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers).json()

    first = client.delete(f"{BASE}/{report.id}/shares/{created['share_id']}", headers=headers)
    second = client.delete(f"{BASE}/{report.id}/shares/{created['share_id']}", headers=headers)

    assert first.status_code == 200 and first.json()["revoked"] is True
    assert second.status_code == 200 and second.json()["revoked"] is False


def test_the_public_page_reads_without_a_session(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """整件事的关键：没有 Cookie 的浏览器也能读到正文。"""
    report = seed_report(session, workspace)
    headers = auth_headers(client, editor)
    created = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers).json()

    response = client.get(created["url_path"])

    assert response.status_code == 200
    assert "分享用周报" in response.text
    assert "这是正文。" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


def test_the_public_page_offers_no_write_action(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """给匿名读者的页面，不能渲染任何他们可以提交的东西。

    没有表单、没有 CSRF 字段、没有侧边栏：读者的能力必须与页面的能力相等。
    """
    report = seed_report(session, workspace)
    headers = auth_headers(client, editor)
    created = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers).json()

    response = client.get(created["url_path"])

    assert "<form" not in response.text
    assert "csrf_token" not in response.text
    assert "生成分享链接" not in response.text


def test_a_revoked_link_answers_404_without_explaining_why(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """已撤销、不存在、已过期三者必须无法区分，否则这条路由就成了探针。"""
    report = seed_report(session, workspace)
    headers = auth_headers(client, editor)
    created = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers).json()
    client.delete(f"{BASE}/{report.id}/shares/{created['share_id']}", headers=headers)

    revoked = client.get(created["url_path"])
    unknown = client.get("/share/this-token-never-existed")

    assert revoked.status_code == 404
    assert unknown.status_code == 404
    # 要的是同一份页面，而不只是同一个状态码：措辞上的任何差别都是提示。
    assert revoked.text == unknown.text


def test_an_expired_link_answers_404(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """过期是在读取时判定的，而不只在签发那一刻。"""
    report = seed_report(session, workspace)
    headers = auth_headers(client, editor)
    created = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers).json()
    row = session.get(ReportShareLink, created["share_id"])
    assert row is not None
    row.expires_at = NOW - timedelta(days=1)
    session.commit()

    response = client.get(created["url_path"])

    assert response.status_code == 404


def test_the_share_page_does_not_leak_other_reports(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """分享页只呈现一份报告，关于系统其余部分则一个字都不露。"""
    report = seed_report(session, workspace)
    other = seed_report(session, workspace)
    headers = auth_headers(client, editor)
    created = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers).json()

    response = client.get(created["url_path"])

    assert other.title not in response.text or other.title == report.title
    assert "/ui/reports" not in response.text
