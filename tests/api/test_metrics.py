"""Integration tests for the observability endpoints.

They drive the real FastAPI application over the real aggregation: `/api/v1/metrics/overview`
for the dashboard payload and `/api/v1/agent/runs` for the ledger and the trajectory. What is
checked is the acceptance itself - tasks, tokens, latency and failure rate are visible, the
window is adjustable and bounded, a viewer is refused - plus the two error-contract details that
are easy to get wrong: a missing run is a `404` with a `code`, and `/runs` is not swallowed by
the `/runs/{run_id}` path parameter.
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
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """An administrator, who may read the overview."""
    return create_actor(session, username="admin", role="ADMIN")


@pytest.fixture()
def editor(session: Session) -> Actor:
    """An editor, who may also read the overview."""
    return create_actor(session, username="editor", role="EDITOR", data_scope="team")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """A read-only user, who may not."""
    return create_actor(session, username="viewer", role="VIEWER")


@pytest.fixture()
def ledger(session: Session, workspace) -> SeededLedger:  # type: ignore[no-untyped-def]
    """Seed the run ledger the dashboard reports on."""
    return seed_ledger(session, workspace)


def test_overview_requires_a_session(client: TestClient) -> None:
    """The overview is not public, even though it contains no report text."""
    assert client.get(METRICS).status_code == 401


@pytest.mark.parametrize("role", ["admin", "editor"])
def test_overview_is_available_to_the_roles_that_may_see_it(
    client: TestClient, request: pytest.FixtureRequest, role: str
) -> None:
    """Both operations roles get the payload, so the guard is not admin-only."""
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
    """A viewer may not read cross-topic cost, failure reasons, or trajectories."""
    headers = auth_headers(client, viewer)
    assert client.get(METRICS, headers=headers).status_code == 403
    assert client.get(RUNS, headers=headers).status_code == 403
    assert client.get(f"{RUNS}/{ledger.failing_run_id}", headers=headers).status_code == 403
    assert client.get(f"{RUNS}/{ledger.failing_run_id}/steps", headers=headers).status_code == 403


def test_overview_reports_tasks_tokens_latency_and_failure_rate(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """The four things the T15 acceptance names are all present, and all correct."""
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
    """With no sweep recorded the section is marked unavailable rather than shown as green."""
    body = client.get(METRICS, headers=auth_headers(client, admin)).json()
    assert body["evaluations"]["dataset"] is None
    assert body["evaluations"]["passed"] is None
    assert body["evaluations"]["targets"] == []


def test_the_window_is_adjustable(client: TestClient, admin: Actor, ledger: SeededLedger) -> None:
    """A wider window reaches rows a narrower one leaves out."""
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
    """The endpoint enforces the documented 1-90 bound the same way it enforces `page_size`."""
    headers = auth_headers(client, admin)
    response = client.get(METRICS, params={"window_days": value}, headers=headers)
    assert response.status_code == 422


def test_the_ledger_is_newest_first_and_filterable(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """The list an operator scans is ordered, total-bearing, and filterable by status."""
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
    """One row answers "what did this run cost and how did it end" without a second call."""
    headers = auth_headers(client, admin)
    body = client.get(f"{RUNS}/{ledger.completed_run_id}", headers=headers).json()
    assert body["status"] == "completed"
    assert body["total_tokens"] == 1_500
    assert body["estimated_cost"] == "0.012300"
    assert body["publish_mode"] == "AUTO_WITH_DELAY"
    assert body["latency_ms"] == 40_000
    assert body["error_code"] is None


def test_a_missing_run_is_not_found_with_a_code(client: TestClient, admin: Actor) -> None:
    """The domain error survives the HTTP layer as RFC 7807 rather than a bare 404."""
    response = client.get(f"{RUNS}/987654321", headers=auth_headers(client, admin))
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_the_run_routes_are_not_shadowed_by_the_path_parameter(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """`/runs` is declared before `/{run_id}`, so the list is not read as an id."""
    headers = auth_headers(client, admin)
    assert client.get(RUNS, headers=headers).status_code == 200
    assert client.get(f"{RUNS}/{ledger.failing_run_id}", headers=headers).status_code == 200


def test_the_trace_reaches_the_tool_call_that_failed(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """A failed run is traceable to the exact tool call, its status, and its error code."""
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

    # A call whose step link degraded is reported rather than dropped: it is the evidence a
    # trace exists to surface.
    assert [call["error_code"] for call in body["unlinked_tool_calls"]] == [UNLINKED_ERROR]


def test_the_trace_redacts_what_the_logs_redact(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """One rule for what may not be exported, applied on the JSON path too."""
    headers = auth_headers(client, admin)
    response = client.get(f"{RUNS}/{ledger.failing_run_id}/steps", headers=headers)
    assert LEAKED_SECRET not in response.text
    assert "[REDACTED]" in response.text


def test_a_missing_trace_is_not_found(client: TestClient, admin: Actor) -> None:
    """A trace of a run that never existed is a 404, not an empty trajectory."""
    response = client.get(f"{RUNS}/987654321/steps", headers=auth_headers(client, admin))
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_the_ledger_pages_within_the_documented_bound(
    client: TestClient, admin: Actor, ledger: SeededLedger
) -> None:
    """One page and the total, so a client can walk the ledger; an oversized page is refused."""
    headers = auth_headers(client, admin)
    first = client.get(RUNS, params={"page": 1, "page_size": 2}, headers=headers).json()
    second = client.get(RUNS, params={"page": 2, "page_size": 2}, headers=headers).json()
    assert first["page_size"] == 2
    assert not {item["id"] for item in first["items"]} & {item["id"] for item in second["items"]}
    oversized = client.get(RUNS, params={"page_size": MAX_PAGE_SIZE + 1}, headers=headers)
    assert oversized.status_code == 422
