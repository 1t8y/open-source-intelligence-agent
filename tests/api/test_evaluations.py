"""Integration tests for the evaluation API.

They drive the T14 surface over HTTP against the real route, service, and persistence stack: the
dataset list, one dataset's cases, a full five-variant sweep, run detail, and the stored
comparison. The offline model client is used throughout, so no network is touched and the whole
sweep is reproducible.

Two things these tests exist to pin down beyond the happy path:

* **Route order.** `/runs` and `/runs/{run_id}` are literal segments declared before
  `/{dataset_id}/...`. If the order were reversed, `/runs` would be parsed as a dataset id and
  the run list would 404 - a silent, confusing failure that only a test catches.
* **Access control.** An evaluation run executes the Agent against real data, so the whole
  surface is a write-role action (`INTERFACES.md` §2): a viewer must be refused everywhere.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from api_support import Actor, auth_headers, build_client, create_actor
from evaluation_support import (
    build_case_document,
    build_dataset_document,
    import_shipped_dataset,
    seed_eval_world,
)
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace, build_settings

from open_source_intelligence.evaluation import runner as evaluation_runner
from open_source_intelligence.evaluation.dataset import import_dataset, parse_dataset
from open_source_intelligence.evaluation.errors import DatasetVersionConflict
from open_source_intelligence.evaluation.targets import BASELINE_TARGET, TARGET_ORDER

BASE = "/api/v1/evaluations"
RUNS = f"{BASE}/runs"


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """An administrator, who may run and read evaluations."""
    return create_actor(session, username="eval-admin", role="ADMIN")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """A read-only user, who must be refused everywhere on this surface."""
    return create_actor(session, username="eval-viewer", role="VIEWER")


@pytest.fixture()
def dataset_id(session: Session, workspace: Workspace) -> int:
    """Seed the world and import the shipped dataset, returning its id."""
    seed_eval_world(session, workspace)
    return import_shipped_dataset(session)


def _sweep(client: TestClient, actor: Actor, dataset_id: int) -> dict[str, object]:
    """Run every variant over a dataset and return the comparison payload."""
    response = client.post(
        f"{BASE}/{dataset_id}/run",
        json={},
        headers=auth_headers(client, actor),
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------------------
# Datasets and cases
# --------------------------------------------------------------------------------------


def test_the_dataset_list_reports_the_imported_version(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """An operator must be able to see which dataset versions exist."""
    response = client.get(BASE, headers=auth_headers(client, admin))

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["id"] for item in items] == [dataset_id]
    assert items[0]["name"] == "weekly-agent-quality"
    assert items[0]["version"] == "v1"


def test_the_case_list_exposes_topics_and_tags(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """The samples of a dataset version must be inspectable before spending a sweep on it."""
    response = client.get(f"{BASE}/{dataset_id}/cases", headers=auth_headers(client, admin))

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    # `list_cases` orders by `case_key`, which is the only deterministic order `eval_cases`
    # offers; the dataset author's file order is not persisted.
    assert [item["case_key"] for item in items] == [
        "growth-surge",
        "injection-readme",
        "missing-readme",
    ]
    assert all(item["enabled"] for item in items)
    injection = next(item for item in items if item["case_key"] == "injection-readme")
    assert "prompt-injection" in injection["tags"]


def test_disabled_cases_can_be_excluded(
    client: TestClient,
    session: Session,
    admin: Actor,
    workspace: Workspace,
) -> None:
    """`enabled_only` must actually filter, since the runner skips disabled cases."""
    seed_eval_world(session, workspace)
    document = build_dataset_document(
        name="mixed",
        cases=[
            build_case_document(case_key="live"),
            build_case_document(case_key="parked", enabled=False),
        ],
    )
    parked_id = import_dataset(session, parse_dataset(document)).dataset_id
    headers = auth_headers(client, admin)

    everything = client.get(f"{BASE}/{parked_id}/cases", headers=headers)
    enabled = client.get(f"{BASE}/{parked_id}/cases?enabled_only=true", headers=headers)

    assert [item["case_key"] for item in everything.json()["items"]] == ["live", "parked"]
    assert [item["case_key"] for item in enabled.json()["items"]] == ["live"]


# --------------------------------------------------------------------------------------
# The unified sweep
# --------------------------------------------------------------------------------------


def test_a_sweep_returns_every_variant_in_one_report(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """`REQUIREMENTS.md` FR-A-06: one request, one comparable report across all variants."""
    body = _sweep(client, admin, dataset_id)

    assert body["dataset"] == {"id": dataset_id, "name": "weekly-agent-quality", "version": "v1"}
    assert body["case_count"] == 3
    assert [target["target_type"] for target in body["targets"]] == list(TARGET_ORDER)
    assert body["passed"] is True


def test_the_report_separates_the_candidate_from_its_controls(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """The Agent is measured against the baseline; each ablation against the Agent."""
    body = _sweep(client, admin, dataset_id)
    by_target = {target["target_type"]: target for target in body["targets"]}

    assert by_target[BASELINE_TARGET]["reference_target"] is None
    assert by_target[BASELINE_TARGET]["deltas_vs_reference"] == {}

    full = by_target["agent_full"]
    assert full["reference_target"] == BASELINE_TARGET
    assert full["deltas_vs_reference"]["citation_coverage"] == "1.0000"
    assert full["gate_failures"] == []

    ablated = by_target["agent_no_tools"]
    assert ablated["reference_target"] == "agent_full"
    assert ablated["deltas_vs_reference"]["citation_coverage"] == "-1.0000"
    assert ablated["regression"] is True
    # Two controls lose citations to the Agent, and both must be named in the report.
    assert body["regressions"] == ["agent_no_verifier", "agent_no_tools"]
    # `agent_no_history` keeps its citations, so it must not be flagged as a regression.
    assert by_target["agent_no_history"]["regression"] is False


def test_the_report_carries_the_cost_and_tool_evidence(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """`docs/evaluation.md` §9 asks for token, cost, latency, and failure evidence."""
    body = _sweep(client, admin, dataset_id)
    by_target = {target["target_type"]: target for target in body["targets"]}

    baseline = by_target[BASELINE_TARGET]
    assert baseline["tool_call_total"] == 0
    assert baseline["verification_sufficient_rate"] is None
    assert Decimal(baseline["total_cost"]) >= 0

    full = by_target["agent_full"]
    assert full["tool_call_total"] > 0
    assert full["tool_call_denied"] == 0
    assert full["prompt_injection_block_rate"] == "1.0000"
    assert Decimal(full["verification_sufficient_rate"]) > 0


def test_a_subset_of_variants_can_be_requested(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """A single-variant run must not be forced to execute the whole sweep."""
    response = client.post(
        f"{BASE}/{dataset_id}/run",
        json={"targets": ["agent_no_tools", BASELINE_TARGET]},
        headers=auth_headers(client, admin),
    )

    assert response.status_code == 200, response.text
    # The report keeps report order, not request order, so two reports stay comparable.
    assert [item["target_type"] for item in response.json()["targets"]] == [
        BASELINE_TARGET,
        "agent_no_tools",
    ]


def test_an_unknown_variant_is_rejected_with_a_problem_document(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """A typo must fail loudly instead of silently running a subset."""
    response = client.post(
        f"{BASE}/{dataset_id}/run",
        json={"targets": ["agent_oops"]},
        headers=auth_headers(client, admin),
    )

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "UNKNOWN_TARGET"
    assert "agent_oops" in body["detail"]


def test_a_missing_dataset_is_a_problem_document(
    client: TestClient,
    admin: Actor,
) -> None:
    """Unknown datasets must produce RFC 7807, not a bare 500 or a stack trace."""
    response = client.post(f"{BASE}/424242/run", json={}, headers=auth_headers(client, admin))

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "NOT_FOUND"


def test_evaluation_can_be_disabled_by_configuration(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator must be able to switch the subsystem off without touching code."""
    monkeypatch.setattr(
        evaluation_runner,
        "get_settings",
        lambda: build_settings(eval_enabled=False),
    )

    response = client.post(f"{BASE}/{dataset_id}/run", json={}, headers=auth_headers(client, admin))

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "EVALUATION_DISABLED"


def test_a_dataset_version_cannot_be_overwritten(
    client: TestClient,
    session: Session,
    admin: Actor,
    workspace: Workspace,
) -> None:
    """Changing a frozen dataset means publishing a new version (`docs/evaluation.md` §2.3)."""
    seed_eval_world(session, workspace)
    first = parse_dataset(build_dataset_document(name="frozen", cases=[build_case_document()]))
    imported = import_dataset(session, first).dataset_id
    changed = parse_dataset(
        build_dataset_document(
            name="frozen",
            cases=[build_case_document(case_key="different")],
        )
    )
    with pytest.raises(DatasetVersionConflict) as excinfo:
        import_dataset(session, changed)
    assert excinfo.value.code == "DATASET_VERSION_CONFLICT"
    # The rejected import must not have disturbed the stored version.
    response = client.get(f"{BASE}/{imported}/cases", headers=auth_headers(client, admin))
    assert response.status_code == 200, response.text
    assert [item["case_key"] for item in response.json()["items"]] == ["case-a"]


# --------------------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------------------


def test_the_run_list_is_addressable_by_dataset_and_variant(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """The literal `/runs` segment must resolve before `/{dataset_id}` can absorb it."""
    _sweep(client, admin, dataset_id)
    headers = auth_headers(client, admin)

    everything = client.get(f"{RUNS}?dataset_id={dataset_id}", headers=headers)
    agent_only = client.get(
        f"{RUNS}?dataset_id={dataset_id}&target_type=agent_full", headers=headers
    )

    assert everything.status_code == 200, everything.text
    assert len(everything.json()["items"]) == len(TARGET_ORDER)
    assert [item["target_type"] for item in agent_only.json()["items"]] == ["agent_full"]


def test_run_detail_carries_every_case_measurement(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """`GET /runs/{run_id}` is the failure-triage entry point: which case, which reason."""
    _sweep(client, admin, dataset_id)
    headers = auth_headers(client, admin)
    runs = client.get(f"{RUNS}?dataset_id={dataset_id}", headers=headers).json()["items"]
    agent_run = next(item for item in runs if item["target_type"] == "agent_full")

    response = client.get(f"{RUNS}/{agent_run['id']}", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["agent_config_id"] is not None
    assert body["summary"]["target_type"] == "agent_full"
    assert len(body["results"]) == 3
    assert {item["case_key"] for item in body["results"]} == {
        "growth-surge",
        "missing-readme",
        "injection-readme",
    }
    assert all(item["citation_coverage"] == "1.0000" for item in body["results"])


def test_the_baseline_run_has_no_agent_config(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """A single-prompt baseline is not an agent configuration, and must not pretend to be one."""
    _sweep(client, admin, dataset_id)
    headers = auth_headers(client, admin)
    runs = client.get(f"{RUNS}?dataset_id={dataset_id}", headers=headers).json()["items"]
    baseline = next(item for item in runs if item["target_type"] == BASELINE_TARGET)

    assert baseline["agent_config_id"] is None
    assert baseline["started_at"] is not None
    assert baseline["finished_at"] >= baseline["started_at"]


def test_an_unknown_run_is_a_problem_document(client: TestClient, admin: Actor) -> None:
    """Triage must not crash on a stale link."""
    response = client.get(f"{RUNS}/987654", headers=auth_headers(client, admin))

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------------------
# The stored comparison
# --------------------------------------------------------------------------------------


def test_the_comparison_is_reproducible_without_rerunning(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """`docs/evaluation.md` §9's report must be readable from storage, not only re-derivable."""
    first = _sweep(client, admin, dataset_id)
    headers = auth_headers(client, admin)
    runs_before = len(
        client.get(f"{RUNS}?dataset_id={dataset_id}", headers=headers).json()["items"]
    )

    response = client.get(f"{BASE}/{dataset_id}/comparison", headers=headers)

    assert response.status_code == 200, response.text
    again = response.json()
    assert [item["target_type"] for item in again["targets"]] == [
        item["target_type"] for item in first["targets"]
    ]
    for before, after in zip(first["targets"], again["targets"], strict=True):
        assert before["metrics"] == after["metrics"]
        assert before["deltas_vs_reference"] == after["deltas_vs_reference"]
    runs_after = len(client.get(f"{RUNS}?dataset_id={dataset_id}", headers=headers).json()["items"])
    assert runs_after == runs_before


def test_comparing_a_dataset_without_a_run_is_a_problem_document(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """An empty report would look like a pass; it must be an explicit 404 instead."""
    response = client.get(f"{BASE}/{dataset_id}/comparison", headers=auth_headers(client, admin))

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "NOT_FOUND"


def test_the_comparison_payload_is_json_stable(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """Rates travel as strings so a client never has to guess at float rounding."""
    body = _sweep(client, admin, dataset_id)

    for target in body["targets"]:
        for value in target["metrics"].values():
            assert value is None or isinstance(value, str)
        assert isinstance(target["total_cost"], str)
        Decimal(target["total_cost"])  # a string a client can parse without float rounding
        assert isinstance(target["tool_call_total"], int)
    # Exactly the controls and the baseline miss the absolute targets; the Agent does not.
    assert set(body["failed_gates"]) == {BASELINE_TARGET, "agent_no_verifier", "agent_no_tools"}


# --------------------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", BASE),
        ("get", f"{BASE}/1/cases"),
        ("get", f"{BASE}/1/comparison"),
        ("get", RUNS),
        ("get", f"{RUNS}/1"),
        ("post", f"{BASE}/1/run"),
    ],
)
def test_a_viewer_is_refused_everywhere(
    client: TestClient,
    viewer: Actor,
    method: str,
    path: str,
) -> None:
    """Running an evaluation against real data is an operator action, not a read."""
    headers = auth_headers(client, viewer)
    response = (
        client.get(path, headers=headers)
        if method == "get"
        else client.post(path, json={}, headers=headers)
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "FORBIDDEN"


def test_the_sweep_is_refused_without_credentials(client: TestClient, dataset_id: int) -> None:
    """No token, no evaluation."""
    response = client.post(f"{BASE}/{dataset_id}/run", json={})

    assert response.status_code == 401, response.text
    assert response.json()["code"] == "AUTH_REQUIRED"
