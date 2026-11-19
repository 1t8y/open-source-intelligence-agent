"""守护两件让「凌晨发出的报告」在页面上找不到的事。

库里的时间一律是 naive UTC，而读者在 UTC+8。界面过去直接 `strftime` 那个值，于是
`2026-11-18 18:55 UTC` 被当成前一天傍晚读，而它其实是 11-19 凌晨的那封邮件；主题页
更是只有配置表单，从主题进来根本看不到它产出过什么。这里从两个方向钉住修复：
过滤器把 UTC 换算成本地时间并带上偏移量，主题页列出属于自己的报告。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from api_support import Actor, build_client, create_actor, web_login
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace

from open_source_intelligence.web.router import _localdt


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """主题页与报告列表都对管理员可见，探针统一用管理员。"""
    return create_actor(session, username="topic-report-admin", role="ADMIN", data_scope="team")


def test_localdt_rolls_utc_into_the_next_local_day() -> None:
    """18:55 UTC 是次日 02:55（+0800）；这正是「凌晨那封邮件」对不上号的地方。"""
    assert _localdt(datetime(2026, 11, 18, 18, 55, 54)) == "2026-11-19 02:55 +0800"


def test_localdt_accepts_the_iso_strings_the_sweep_returns() -> None:
    """发布清扫返回的是 ISO 字符串，与数据库里的 datetime 必须走同一个口径。"""
    assert _localdt("2026-11-18T18:55:54.392205+00:00") == "2026-11-19 02:55 +0800"


def test_localdt_renders_a_dash_for_missing_values() -> None:
    """未发布/未生成的时间在页面上是破折号，而不是「None」或空串。"""
    assert _localdt(None) == "—"


def test_the_reports_list_shows_publish_time_in_local_time(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """列表新增「发布时间」列，且按本地时区渲染 —— 否则凌晨的报告永远显示成前一天。"""
    topic = workspace.add_topic(name="local-time-topic")
    run = workspace.add_agent_run(topic.id)
    report = workspace.add_report(
        topic_id=topic.id,
        agent_run_id=run.id,
        title="LOCAL-TIME-REPORT",
        status="published",
    )
    report.published_at = datetime(2026, 11, 18, 18, 55, 54, tzinfo=UTC)
    session.commit()

    assert web_login(client, admin).status_code == 303
    response = client.get("/ui/reports")

    assert response.status_code == 200
    assert "发布时间" in response.text
    assert "2026-11-19 02:55 +0800" in response.text


def test_the_topic_page_lists_only_its_own_reports(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """主题页能回答「这个主题产出过什么」，且不会串到别的主题。"""
    topic = workspace.add_topic(name="topic-with-reports")
    other = workspace.add_topic(name="topic-without-reports")
    own_run = workspace.add_agent_run(topic.id)
    other_run = workspace.add_agent_run(other.id)
    workspace.add_report(
        topic_id=topic.id,
        agent_run_id=own_run.id,
        title="THIS-TOPIC-REPORT",
        status="published",
    )
    workspace.add_report(
        topic_id=other.id,
        agent_run_id=other_run.id,
        title="OTHER-TOPIC-REPORT",
        status="published",
    )
    session.commit()

    assert web_login(client, admin).status_code == 303
    response = client.get(f"/ui/topics/{topic.id}")

    assert response.status_code == 200
    assert "这个主题的报告" in response.text
    assert "THIS-TOPIC-REPORT" in response.text
    assert "OTHER-TOPIC-REPORT" not in response.text


def test_the_topic_page_explains_an_empty_report_history(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """还没产出报告的主题要说清「为什么没有」，而不是留一块空白。"""
    topic = workspace.add_topic(name="fresh-topic")
    session.commit()

    assert web_login(client, admin).status_code == 303
    response = client.get(f"/ui/topics/{topic.id}")

    assert response.status_code == 200
    assert "这个主题还没有产出报告" in response.text
