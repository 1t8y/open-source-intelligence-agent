"""Integration tests for the operations dashboard.

The dashboard is the same data as `/api/v1/metrics/overview` rendered for an operator, so these
tests check the two things a page adds on top: the numbers actually reach the HTML, and the role
guard plus the navigation entry agree with each other. A viewer must be refused the page *and*
must not be shown a link to it, because a dead link reads as a broken product rather than as a
deliberate restriction.
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
NAV_ENTRY = '<a href="/ui/dashboard">运维看板</a>'


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """An administrator, who may open the dashboard."""
    return create_actor(session, username="admin", role="ADMIN")


@pytest.fixture()
def editor(session: Session) -> Actor:
    """An editor, who may also open the dashboard."""
    return create_actor(session, username="editor", role="EDITOR", data_scope="team")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """A read-only user, who may not."""
    return create_actor(session, username="viewer", role="VIEWER")


@pytest.fixture()
def ledger(session: Session, workspace) -> SeededLedger:  # type: ignore[no-untyped-def]
    """Seed the run ledger the dashboard reports on."""
    return seed_ledger(session, workspace)


def test_anonymous_browsing_redirects_to_login(client: TestClient) -> None:
    """The dashboard is behind the same cookie session as the rest of the interface."""
    response = client.get(DASHBOARD, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login?next=%2Fui%2Fdashboard"


@pytest.mark.parametrize("role", ["admin", "editor"])
def test_both_operations_roles_can_open_the_dashboard(
    client: TestClient, request: pytest.FixtureRequest, role: str, ledger: SeededLedger
) -> None:
    """The guard follows `METRICS_ROLES`, so it is not an admin-only page."""
    actor: Actor = request.getfixturevalue(role)
    web_login(client, actor)
    response = client.get(DASHBOARD)
    assert response.status_code == 200
    assert response.text.lstrip().startswith("<!DOCTYPE html>")
    assert NAV_ENTRY in response.text


def test_the_dashboard_shows_the_numbers_the_service_computed(
    client: TestClient, admin: Actor, session: Session, ledger: SeededLedger
) -> None:
    """The page is searched for what the aggregation produced, not for constants.

    Recomputing the expected values through the service rather than typing them here is what
    makes this a check on the template: a template that printed a wrong field would disagree
    with the payload the same request served.
    """
    web_login(client, admin)
    html = client.get(DASHBOARD, params={"window_days": 7}).text

    overview = collect_metrics(session, now=utc_now(), window_days=7)
    assert str(overview.agent.total_tokens) in html
    assert str(overview.pipeline.total) in html
    assert f"{float(overview.agent.success_rate) * 100:.1f}%" in html
    assert f"{float(overview.reports.manual_block_rate) * 100:.1f}%" in html
    assert f"{overview.agent.latency.p95_ms} ms" in html
    # The cards the acceptance names, in the order an operator reads them.
    headings = ("任务执行", "Agent 运行", "工具调用（白名单）", "报告产出", "评测结果", "最近运行")
    for heading in headings:
        assert heading in html


def test_the_window_can_be_switched_from_the_page(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """The switcher offers the three windows and each one renders its own numbers."""
    web_login(client, admin)
    week = client.get(DASHBOARD, params={"window_days": 7})
    month = client.get(DASHBOARD, params={"window_days": 30})
    assert week.status_code == 200
    for option in (1, 7, 30):
        assert f"/ui/dashboard?window_days={option}" in week.text
    assert "7 天" in week.text
    assert "30 天" in month.text
    # A wider window reaches a run the narrower one leaves out, so the pages differ.
    assert week.text != month.text


def test_an_empty_window_renders_a_dash_rather_than_a_zero(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """The absent-versus-zero rule as an operator experiences it.

    The clock is overridden for this one request: any window ending at the real clock holds the
    seeded rows, so an empty page is only reachable by asking the page for a window in which
    nothing exists. That is the same future window the smoke probes the aggregation with.
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
    """A viewer gets an explanation, and the navigation never advertises the page to them."""
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
    """Refusing the dashboard does not narrow the report permissions a viewer already had."""
    web_login(client, viewer)
    assert client.get("/ui/reports").status_code == 200


def test_the_dashboard_links_to_a_run_trace(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """The recent-runs table is the entry point to the trajectory, not a dead end."""
    web_login(client, admin)
    html = client.get(DASHBOARD).text
    assert f"/ui/dashboard/runs/{ledger.failing_run_id}" in html
    assert f"#{ledger.failing_run_id}" in html


def test_the_trace_page_reaches_the_tool_call_that_failed(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """The operator-facing half of the same acceptance the API test covers."""
    web_login(client, admin)
    response = client.get(f"{DASHBOARD}/runs/{ledger.failing_run_id}")
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
    """The page is an egress path too, so it applies the same rule the endpoint does."""
    web_login(client, admin)
    text = client.get(f"{DASHBOARD}/runs/{ledger.failing_run_id}").text
    assert LEAKED_SECRET not in text
    assert "[REDACTED]" in text


def test_a_missing_run_renders_the_not_found_page(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """A bad id is a rendered explanation, matching how the report pages behave."""
    web_login(client, admin)
    response = client.get(f"{DASHBOARD}/runs/987654321")
    assert response.status_code == 404
    assert "不存在" in response.text


def test_a_viewer_cannot_open_a_trace_either(
    client: TestClient, viewer: Actor, ledger: SeededLedger
) -> None:
    """The drill-down repeats the guard, so the page is not reachable by guessing a URL."""
    web_login(client, viewer)
    response = client.get(f"{DASHBOARD}/runs/{ledger.failing_run_id}")
    assert response.status_code == 403
    assert DENIED_TOOL not in response.text
