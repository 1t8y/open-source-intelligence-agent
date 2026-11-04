"""守护可观测性端点：把真实的聚合结果原样暴露成面板与运行清单。

用例走真实 FastAPI 应用加真实聚合：`/api/v1/metrics/overview` 出面板载荷，
`/api/v1/agent/runs` 出账本与轨迹。校验的就是验收条件本身 —— 任务、token、时延、
失败率都看得见，窗口可调且有界，只读用户被拒绝 —— 外加两处容易写错的错误契约
细节：不存在的运行是带 `code` 的 `404`；`/runs` 不会被 `/runs/{run_id}` 的路径
参数吞掉。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from api_support import Actor, auth_headers, build_client, create_actor
from fastapi.testclient import TestClient
from observability_support import (
    DENIED_ERROR,
    DENIED_TOOL,
    LEAKED_SECRET,
    UNLINKED_ERROR,
    SeededLedger,
    seed_ledger,
)
from sqlalchemy.orm import Session, sessionmaker

from open_source_intelligence.observability import metrics
from open_source_intelligence.observability.queries import MAX_PAGE_SIZE

METRICS = "/api/v1/metrics/overview"
RUNS = "/api/v1/agent/runs"


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """管理员：可以读总览。"""
    return create_actor(session, username="admin", role="ADMIN")


@pytest.fixture()
def editor(session: Session) -> Actor:
    """编辑：同样可以读总览。"""
    return create_actor(session, username="editor", role="EDITOR", data_scope="team")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """只读用户：读不了。"""
    return create_actor(session, username="viewer", role="VIEWER")


@pytest.fixture()
def ledger(session: Session, workspace) -> SeededLedger:  # type: ignore[no-untyped-def]
    """铺好面板要统计的那份运行账本。"""
    return seed_ledger(session, workspace)


def test_overview_requires_a_session(client: TestClient) -> None:
    """总览不公开，尽管它并不包含任何报告正文。"""
    assert client.get(METRICS).status_code == 401


@pytest.mark.parametrize("role", ["admin", "editor"])
def test_overview_is_available_to_the_roles_that_may_see_it(
    client: TestClient, request: pytest.FixtureRequest, role: str
) -> None:
    """两个运维角色都拿得到载荷，说明守卫不是只管管理员。"""
    actor: Actor = request.getfixturevalue(role)
    response = client.get(METRICS, headers=auth_headers(client, actor))
    assert response.status_code == 200
    assert sorted(response.json()) == [
        "agent",
        "evaluations",
        "pipeline",
        "reports",
        "tools",
        "window",
    ]


def test_a_viewer_is_refused_the_overview_and_the_ledger(
    client: TestClient, viewer: Actor, ledger: SeededLedger
) -> None:
    """只读用户不能读跨主题成本、失败原因和轨迹。"""
    headers = auth_headers(client, viewer)
    assert client.get(METRICS, headers=headers).status_code == 403
    assert client.get(RUNS, headers=headers).status_code == 403
    assert client.get(f"{RUNS}/{ledger.failing_run_id}", headers=headers).status_code == 403
    assert client.get(f"{RUNS}/{ledger.failing_run_id}/steps", headers=headers).status_code == 403


def test_overview_reports_tasks_tokens_latency_and_failure_rate(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """T15 验收点名的四样东西都在，而且都对。"""
    headers = auth_headers(client, admin)
    body = client.get(METRICS, params={"window_days": 7}, headers=headers).json()

    # 任务
    assert body["pipeline"]["total"] == 5
    assert body["pipeline"]["by_status"] == {"failed": 2, "succeeded": 3}
    assert body["pipeline"]["success_rate"] == "0.6000"
    assert sorted(body["pipeline"]["by_run_type"]) == ["agent", "collect", "rank"]

    # Token
    assert body["agent"]["total_tokens"] == 3_050
    assert body["agent"]["prompt_tokens"] == 2_500
    assert body["agent"]["completion_tokens"] == 550
    assert body["agent"]["tokens_per_run"] == "1016.6667"

    # 延迟
    assert body["agent"]["latency"] == {"samples": 3, "p50_ms": 60_000, "p95_ms": 80_000}
    assert body["pipeline"]["latency"] == {"samples": 5, "p50_ms": 40_000, "p95_ms": 80_000}

    # 失败率与失败原因
    assert body["agent"]["success_rate"] == "0.6667"
    assert body["agent"]["failure_reasons"] == {"AGENT_TOOL_BUDGET_EXCEEDED": 1}
    assert body["tools"]["success_rate"] == "0.3333"
    assert body["tools"]["by_status"] == {"denied": 1, "failed": 1, "success": 1}

    # 报告
    assert body["reports"]["manual_block_rate"] == "0.5000"


def test_overview_reuses_the_evaluation_verdict(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """没有记录过扫描时，这一段要标成「不可用」，而不是显示成一片绿。"""
    body = client.get(METRICS, headers=auth_headers(client, admin)).json()
    assert body["evaluations"]["dataset"] is None
    assert body["evaluations"]["passed"] is None
    assert body["evaluations"]["targets"] == []


def test_the_window_is_adjustable(client: TestClient, admin: Actor, ledger: SeededLedger) -> None:
    """更宽的窗口能覆盖窄窗口漏掉的行。"""
    headers = auth_headers(client, admin)
    week = client.get(METRICS, params={"window_days": 7}, headers=headers).json()
    month = client.get(METRICS, params={"window_days": 30}, headers=headers).json()
    assert week["window"]["days"] == 7
    assert week["agent"]["total"] == 3
    assert month["agent"]["total"] == 4
    assert month["pipeline"]["total"] == 6


@pytest.mark.parametrize("value", [0, -1, metrics.MAX_WINDOW_DAYS + 1, 999])
def test_an_out_of_range_window_is_refused_rather_than_clamped(
    client: TestClient, admin: Actor, value: int
) -> None:
    """端点用管 `page_size` 的同一套方式，执行文档写明的 1–90 边界。"""
    headers = auth_headers(client, admin)
    response = client.get(METRICS, params={"window_days": value}, headers=headers)
    assert response.status_code == 422


def test_the_ledger_is_newest_first_and_filterable(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """运维者要扫的这份列表有排序、带总数，并且能按状态筛选。"""
    headers = auth_headers(client, admin)
    page = client.get(RUNS, headers=headers).json()
    assert page["total"] == 5
    assert [item["id"] for item in page["items"]] == sorted(
        (item["id"] for item in page["items"]), reverse=True
    )

    filtered = client.get(RUNS, params={"status": "failed"}, headers=headers).json()
    assert filtered["total"] == 2
    assert {item["status"] for item in filtered["items"]} == {"failed"}
    assert ledger.failing_run_id in {item["id"] for item in filtered["items"]}


def test_the_ledger_carries_budget_and_outcome_fields(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """一行就要答出「这次运行花了多少、怎么结束的」，不必再发一次请求。"""
    headers = auth_headers(client, admin)
    body = client.get(f"{RUNS}/{ledger.completed_run_id}", headers=headers).json()
    assert body["status"] == "completed"
    assert body["total_tokens"] == 1_500
    assert body["estimated_cost"] == "0.012300"
    assert body["publish_mode"] == "AUTO_WITH_DELAY"
    assert body["latency_ms"] == 40_000
    assert body["error_code"] is None


def test_a_missing_run_is_not_found_with_a_code(client: TestClient, admin: Actor) -> None:
    """领域错误穿过 HTTP 层后仍是 RFC 7807，而不是一个裸 404。"""
    response = client.get(f"{RUNS}/987654321", headers=auth_headers(client, admin))
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_the_run_routes_are_not_shadowed_by_the_path_parameter(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """`/runs` 声明在 `/{run_id}` 之前，所以清单不会被当成一个 id。"""
    headers = auth_headers(client, admin)
    assert client.get(RUNS, headers=headers).status_code == 200
    assert client.get(f"{RUNS}/{ledger.failing_run_id}", headers=headers).status_code == 200


def test_the_trace_reaches_the_tool_call_that_failed(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """失败的运行能追到具体那一次工具调用、它的状态与错误码。"""
    body = client.get(
        f"{RUNS}/{ledger.failing_run_id}/steps", headers=auth_headers(client, admin)
    ).json()

    assert body["run"]["status"] == "failed"
    assert body["run"]["error_code"] == "AGENT_TOOL_BUDGET_EXCEEDED"
    assert [step["step_index"] for step in body["steps"]] == [1, 2]
    assert [step["step_type"] for step in body["steps"]] == ["plan", "tool"]

    denied = next(
        call for step in body["steps"] for call in step["tool_calls"] if call["status"] == "denied"
    )
    assert denied["tool_name"] == DENIED_TOOL
    assert denied["error_code"] == DENIED_ERROR
    assert denied["latency_ms"] == 800

    # 步骤关联已经损坏的调用要报出来而不是丢掉：它正是轨迹存在的意义所在。
    assert [call["error_code"] for call in body["unlinked_tool_calls"]] == [UNLINKED_ERROR]


def test_the_trace_redacts_what_the_logs_redact(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """不能出网的字段只有一条规则，在 JSON 这条路径上同样要执行。"""
    headers = auth_headers(client, admin)
    response = client.get(f"{RUNS}/{ledger.failing_run_id}/steps", headers=headers)
    assert LEAKED_SECRET not in response.text
    assert "[REDACTED]" in response.text


def test_a_missing_trace_is_not_found(client: TestClient, admin: Actor) -> None:
    """从没存在过的运行，其轨迹是 404，而不是一条空轨迹。"""
    response = client.get(f"{RUNS}/987654321/steps", headers=auth_headers(client, admin))
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_the_ledger_pages_within_the_documented_bound(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """一页加一个总数，客户端就能走完账本；超长的一页则被拒绝。"""
    headers = auth_headers(client, admin)
    first = client.get(RUNS, params={"page": 1, "page_size": 2}, headers=headers).json()
    second = client.get(RUNS, params={"page": 2, "page_size": 2}, headers=headers).json()
    assert first["page_size"] == 2
    assert not {item["id"] for item in first["items"]} & {item["id"] for item in second["items"]}
    oversized = client.get(RUNS, params={"page_size": MAX_PAGE_SIZE + 1}, headers=headers)
    assert oversized.status_code == 422
