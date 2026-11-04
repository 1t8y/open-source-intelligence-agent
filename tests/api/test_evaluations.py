"""守护评测 API：数据集清单、用例清单、五变体总扫、运行详情与已存对比。

它们针对真实的路由、服务与持久化层，用 HTTP 跑通 T14 这条链路；全程使用离线
模型客户端，因此不碰网络，整次扫描都可以复现。

除了顺利路径，这组用例还要钉住两件事：

* **路由顺序。** `/runs` 与 `/runs/{run_id}` 是声明在
  `/{dataset_id}/...` 之前的字面段。顺序一旦颠倒，`/runs` 就会被当成数据集
  id 解析，运行清单随之 404 —— 这种悄无声息的故障只有测试能拦住。
* **访问控制。** 一次评测运行会拿真实数据驱动 Agent，所以整条链路都是写角色
  的动作（`INTERFACES.md` §2）：只读用户在任何一处都必须被拒绝。
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
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """管理员：既能发起评测，也能读评测结果。"""
    return create_actor(session, username="eval-admin", role="ADMIN")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """只读用户：在这条链路的任何一处都必须被拒绝。"""
    return create_actor(session, username="eval-viewer", role="VIEWER")


@pytest.fixture()
def dataset_id(session: Session, workspace: Workspace) -> int:
    """铺好世界并导入随包发布的数据集，返回它的 id。"""
    seed_eval_world(session, workspace)
    return import_shipped_dataset(session)


def _sweep(client: TestClient, actor: Actor, dataset_id: int) -> dict[str, object]:
    """在一个数据集上跑完所有变体，返回对比载荷。"""
    response = client.post(
        f"{BASE}/{dataset_id}/run",
        json={},
        headers=auth_headers(client, actor),
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------------------
# 数据集与用例
# --------------------------------------------------------------------------------------


def test_the_dataset_list_reports_the_imported_version(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """运维者必须能看到现存的是哪些数据集版本。"""
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
    """在为一个数据集版本花掉一次扫描之前，必须先能看清它的样本。"""
    response = client.get(f"{BASE}/{dataset_id}/cases", headers=auth_headers(client, admin))

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    # `list_cases` 按 `case_key` 排序，这是 `eval_cases` 唯一能给出的
    # 确定性顺序；数据集作者在文件里的排列顺序并不会被持久化。
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
    """`enabled_only` 必须真的过滤，因为执行器本身会跳过被停用的用例。"""
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
# 统一总扫
# --------------------------------------------------------------------------------------


def test_a_sweep_returns_every_variant_in_one_report(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """`REQUIREMENTS.md` FR-A-06：一次请求，得到一份覆盖全部变体的可比报告。"""
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
    """报告要把候选与它的对照组分开：Agent 对齐基线，各个消融对齐 Agent。"""
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
    # 有两个对照组相对 Agent 丢了引用，两者都必须在报告里被点名。
    assert body["regressions"] == ["agent_no_verifier", "agent_no_tools"]
    # `agent_no_history` 的引用没有掉，所以它不能被标成回归。
    assert by_target["agent_no_history"]["regression"] is False


def test_the_report_carries_the_cost_and_tool_evidence(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """`docs/evaluation.md` §9 要求报告带上 token、成本、时延与失败证据。"""
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
    """只跑单个变体时，不该被迫执行整轮扫描。"""
    response = client.post(
        f"{BASE}/{dataset_id}/run",
        json={"targets": ["agent_no_tools", BASELINE_TARGET]},
        headers=auth_headers(client, admin),
    )

    assert response.status_code == 200, response.text
    # 报告按报告顺序排列，而不是按请求顺序，这样两份报告才始终可比。
    assert [item["target_type"] for item in response.json()["targets"]] == [
        BASELINE_TARGET,
        "agent_no_tools",
    ]


def test_an_unknown_variant_is_rejected_with_a_problem_document(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """写错变体名必须大声失败，而不是悄悄只跑一个子集。"""
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
    """未知数据集要给出 RFC 7807 问题文档，而不是裸 500 或堆栈。"""
    response = client.post(f"{BASE}/424242/run", json={}, headers=auth_headers(client, admin))

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "NOT_FOUND"


def test_evaluation_can_be_disabled_by_configuration(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运维者必须能只改配置就关掉这个子系统，而不必动代码。"""
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
    """改一个已冻结的数据集，只能靠发布新版本（`docs/evaluation.md` §2.3）。"""
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
    # 被拒绝的这次导入不能动到已存的版本。
    response = client.get(f"{BASE}/{imported}/cases", headers=auth_headers(client, admin))
    assert response.status_code == 200, response.text
    assert [item["case_key"] for item in response.json()["items"]] == ["case-a"]


# --------------------------------------------------------------------------------------
# 运行记录
# --------------------------------------------------------------------------------------


def test_the_run_list_is_addressable_by_dataset_and_variant(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """字面的 `/runs` 段必须先被解析，否则它会被 `/{dataset_id}` 吞掉。"""
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
    """`GET /runs/{run_id}` 是排查失败的入口：哪个用例、什么原因。"""
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
    """单提示词基线不是一份 Agent 配置，也不该冒充成一份。"""
    _sweep(client, admin, dataset_id)
    headers = auth_headers(client, admin)
    runs = client.get(f"{RUNS}?dataset_id={dataset_id}", headers=headers).json()["items"]
    baseline = next(item for item in runs if item["target_type"] == BASELINE_TARGET)

    assert baseline["agent_config_id"] is None
    assert baseline["started_at"] is not None
    assert baseline["finished_at"] >= baseline["started_at"]


def test_an_unknown_run_is_a_problem_document(client: TestClient, admin: Actor) -> None:
    """拿着过期的链接去排查，不该把排查页本身弄崩。"""
    response = client.get(f"{RUNS}/987654", headers=auth_headers(client, admin))

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------------------
# 已存的对比
# --------------------------------------------------------------------------------------


def test_the_comparison_is_reproducible_without_rerunning(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """`docs/evaluation.md` §9 的报告必须能从存储里读出来，而不只能重新推导。"""
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
    """空报告看起来会像「全部通过」，所以这里必须是显式的 404。"""
    response = client.get(f"{BASE}/{dataset_id}/comparison", headers=auth_headers(client, admin))

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "NOT_FOUND"


def test_the_comparison_payload_is_json_stable(
    client: TestClient,
    admin: Actor,
    dataset_id: int,
) -> None:
    """比率一律以字符串出行，客户端永远不必去猜浮点舍入。"""
    body = _sweep(client, admin, dataset_id)

    for target in body["targets"]:
        for value in target["metrics"].values():
            assert value is None or isinstance(value, str)
        assert isinstance(target["total_cost"], str)
        Decimal(target["total_cost"])  # 一个能被客户端解析、不必担心浮点舍入的字符串
        assert isinstance(target["tool_call_total"], int)
    # 恰好是基线与两个对照组没达标，Agent 本身达标。
    assert set(body["failed_gates"]) == {BASELINE_TARGET, "agent_no_verifier", "agent_no_tools"}


# --------------------------------------------------------------------------------------
# 访问控制
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
    """拿真实数据跑评测是运维动作，不是读操作。"""
    headers = auth_headers(client, viewer)
    response = (
        client.get(path, headers=headers)
        if method == "get"
        else client.post(path, json={}, headers=headers)
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "FORBIDDEN"


def test_the_sweep_is_refused_without_credentials(client: TestClient, dataset_id: int) -> None:
    """没有令牌，就没有评测。"""
    response = client.post(f"{BASE}/{dataset_id}/run", json={})

    assert response.status_code == 401, response.text
    assert response.json()["code"] == "AUTH_REQUIRED"
