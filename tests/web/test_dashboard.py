"""守护运维面板：同一份数据渲染成页面之后，还剩下哪些必须成立的事。

面板就是把 `/api/v1/metrics/overview` 的数据渲染给运维者看，所以用例只检查页面在
此之上新增的两件事：数字确实进入了 HTML；角色守卫与导航入口彼此一致。只读用户
既必须被拒绝，也不能看到指向它的链接 —— 一条点不通的链接读起来像产品坏了，而不
像一次有意的限制。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from api_support import Actor, build_client, create_actor, web_login
from fastapi.testclient import TestClient
from observability_support import (
    DENIED_ERROR,
    DENIED_TOOL,
    LEAKED_SECRET,
    SeededLedger,
    seed_ledger,
)
from sqlalchemy.orm import Session, sessionmaker

from open_source_intelligence.observability.queries import collect_metrics
from open_source_intelligence.persistence.base import utc_now

DASHBOARD = "/ui/dashboard"
#: 侧边栏里指向面板的那一条。按 href 匹配而不是匹配整个链接元素：文字现在放在
#: 单独的元素里，`aria-current` 标记又随页面进出，所以钉死整段标记只会变成在测
#: 模板的空白字符。
NAV_ENTRY = 'href="/ui/dashboard"'


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """管理员：可以打开面板。"""
    return create_actor(session, username="admin", role="ADMIN")


@pytest.fixture()
def editor(session: Session) -> Actor:
    """编辑：也可以打开面板。"""
    return create_actor(session, username="editor", role="EDITOR", data_scope="team")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """只读用户：打不开。"""
    return create_actor(session, username="viewer", role="VIEWER")


@pytest.fixture()
def ledger(session: Session, workspace) -> SeededLedger:  # type: ignore[no-untyped-def]
    """铺好面板要统计的那份运行账本。"""
    return seed_ledger(session, workspace)


def test_anonymous_browsing_redirects_to_login(client: TestClient) -> None:
    """面板与界面其余部分共用同一套 Cookie 会话。"""
    response = client.get(DASHBOARD, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login?next=%2Fui%2Fdashboard"


@pytest.mark.parametrize("role", ["admin", "editor"])
def test_both_operations_roles_can_open_the_dashboard(
    client: TestClient, request: pytest.FixtureRequest, role: str, ledger: SeededLedger
) -> None:
    """守卫跟随 `METRICS_ROLES`，所以这不是一个只管管理员的页面。"""
    actor: Actor = request.getfixturevalue(role)
    web_login(client, actor)
    response = client.get(DASHBOARD)
    assert response.status_code == 200
    assert response.text.lstrip().startswith("<!DOCTYPE html>")
    assert NAV_ENTRY in response.text


def test_the_dashboard_shows_the_numbers_the_service_computed(
    client: TestClient, admin: Actor, session: Session, ledger: SeededLedger
) -> None:
    """页面上找的是聚合算出来的值，而不是写死的常量。

    期望值通过服务重新算一遍、而不是在这里手敲，这才让用例变成了对模板的检查：
    模板若印错了字段，就会与同一次请求所服务的载荷对不上。
    """
    web_login(client, admin)
    html = client.get(DASHBOARD, params={"window_days": 7}).text

    overview = collect_metrics(session, now=utc_now(), window_days=7)
    assert str(overview.agent.total_tokens) in html
    assert str(overview.pipeline.total) in html
    assert f"{float(overview.agent.success_rate) * 100:.1f}%" in html
    assert f"{float(overview.reports.manual_block_rate) * 100:.1f}%" in html
    assert f"{overview.agent.latency.p95_ms} ms" in html
    # 验收点名的几张卡片，按运维者阅读它们的顺序排列。
    headings = ("任务执行", "Agent 运行", "工具调用（白名单）", "报告产出", "评测结果", "最近运行")
    for heading in headings:
        assert heading in html


def test_the_window_can_be_switched_from_the_page(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """切换器提供三个窗口，每一个都渲染出属于自己的数字。"""
    web_login(client, admin)
    week = client.get(DASHBOARD, params={"window_days": 7})
    month = client.get(DASHBOARD, params={"window_days": 30})
    assert week.status_code == 200
    for option in (1, 7, 30):
        assert f"/ui/dashboard?window_days={option}" in week.text
    assert "7 天" in week.text
    assert "30 天" in month.text
    # 更宽的窗口能覆盖窄窗口漏掉的一次运行，所以两页内容不同。
    assert week.text != month.text


def test_an_empty_window_renders_a_dash_rather_than_a_zero(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """运维者眼中的「缺席」与「零」之别。

    这一次请求把时钟顶掉了：任何以真实时钟结尾的窗口都盖得住夹具那些行，所以只有
    向页面要一个「什么都不存在」的窗口，才可能得到一张空页。这也正是冒烟检查探测
    聚合时用的那个未来窗口。
    """
    web_login(client, admin)
    populated = client.get(DASHBOARD, params={"window_days": 7})
    client.app.dependency_overrides[utc_now] = lambda: utc_now().replace(year=2100)
    try:
        empty = client.get(DASHBOARD, params={"window_days": 7})
    finally:
        client.app.dependency_overrides.clear()

    assert empty.status_code == 200
    assert "metric-missing" in empty.text
    assert empty.text.count("metric-missing") > populated.text.count("metric-missing")


def test_a_viewer_is_refused_and_is_not_offered_the_link(
    client: TestClient, viewer: Actor, ledger: SeededLedger
) -> None:
    """只读用户会得到解释，而导航也从不把这一页推荐给他。"""
    web_login(client, viewer)
    dashboard = client.get(DASHBOARD)
    assert dashboard.status_code == 403
    assert "管理员和编辑者" in dashboard.text

    listing = client.get("/ui/reports")
    assert listing.status_code == 200
    assert NAV_ENTRY not in listing.text


def test_a_viewer_is_still_shown_the_reports_they_may_read(
    client: TestClient, viewer: Actor, ledger: SeededLedger
) -> None:
    """拒绝面板，不能顺带收窄只读用户本来已有的报告权限。"""
    web_login(client, viewer)
    assert client.get("/ui/reports").status_code == 200


def test_the_dashboard_links_to_a_run_trace(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """最近运行那张表是通往轨迹的入口，不是一条死路。"""
    web_login(client, admin)
    html = client.get(DASHBOARD).text
    assert f"/ui/runs/{ledger.failing_run_id}" in html
    assert f"#{ledger.failing_run_id}" in html


def test_the_trace_page_reaches_the_tool_call_that_failed(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """同一条验收里面向运维者的那一半，与 API 测试覆盖的另一半对应。"""
    web_login(client, admin)
    response = client.get(f"/ui/runs/{ledger.failing_run_id}")
    assert response.status_code == 200
    text = response.text
    assert f"运行 #{ledger.failing_run_id}" in text
    assert DENIED_TOOL in text
    assert DENIED_ERROR in text
    assert "未挂到步骤上的工具调用" in text
    assert text.count("trace-step") == 2


def test_the_trace_page_redacts_credentials(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """页面同样是一条出网路径，因此执行与端点相同的那条规则。"""
    web_login(client, admin)
    text = client.get(f"{DASHBOARD}/runs/{ledger.failing_run_id}").text
    assert LEAKED_SECRET not in text
    assert "[REDACTED]" in text


def test_a_missing_run_renders_the_not_found_page(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """坏掉的 id 会渲染成一段解释，与报告页面的行为保持一致。"""
    web_login(client, admin)
    response = client.get(f"{DASHBOARD}/runs/987654321")
    assert response.status_code == 404
    assert "不存在" in response.text


def test_a_viewer_cannot_open_a_trace_either(
    client: TestClient, viewer: Actor, ledger: SeededLedger
) -> None:
    """下钻页重复一次守卫，于是光靠猜 URL 也到不了这一页。"""
    web_login(client, viewer)
    response = client.get(f"{DASHBOARD}/runs/{ledger.failing_run_id}")
    assert response.status_code == 403
    assert DENIED_TOOL not in response.text
