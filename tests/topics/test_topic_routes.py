"""主题端点的 HTTP 用例。

被测契约是 `docs/api-contract.md` §3.2。其中两条性质得到更多关注，
因为它们是契约花了大量篇幅论证的两条，也是路由能在任何单个响应都
看不出问题的情况下破坏的两条：

* **可见性规则要落到数据库上。** 先取出所有主题、再在 Python 里
  丢掉不可见的那些，
  返回的 `items` 会是对的，`total` 却把调用方看不到的主题也算进去了。
  只有 `total` 会露馅，所以 `total` 单独断言。
* **`q` 是子串搜索，不是模式。** 对调用方来说 `%` 与 `_` 就是普通字符，
  所以端点绝不能把它们原样交给 `LIKE` —— 否则 `q=_` 会匹配所有非空行。

文件其余部分是角色矩阵、契约写明的两种失败形态（`403` 且不带元数据、
但审计行点名主题；查不到时 `404`），以及刻意不提供运行触发入口。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from api_support import Actor, auth_headers, build_client, create_actor
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace

from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.intelligence import Topic

TOPICS = "/api/v1/topics"

#: 各个夹具创建的账号名。与服务层用例的夹具区分开，
#: 这样两个模块即使共用同一种数据库形态也不会在用户名上撞车。
USERNAMES = {
    "admin": "topics-route-admin",
    "editor": "topics-route-editor",
    "viewer": "topics-route-viewer",
}

#: 部署可能产生的每一个角色，顺序与下面矩阵遍历的顺序一致。
READING_ROLES = ("ADMIN", "EDITOR", "VIEWER")


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


def _actor(session: Session, key: str, role: str) -> Actor:
    """创建角色夹具要发放的那个账号。"""
    return create_actor(session, username=USERNAMES[key], role=role)


@pytest.fixture()
def admin(session: Session) -> Actor:
    """管理员：唯一允许写主题的角色。"""
    return _actor(session, "admin", "ADMIN")


@pytest.fixture()
def editor(session: Session) -> Actor:
    """编辑者：可以写报告，不能写主题。"""
    return _actor(session, "editor", "EDITOR")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """只读账号。"""
    return _actor(session, "viewer", "VIEWER")


def _payload(**fields: object) -> dict[str, object]:
    """用两个必填字段加上调用方传入的内容拼出创建 / 更新请求体。

    只接受关键字参数，这样调用方覆盖 `name`（边界用例就是这么做的）时，
    它与其他字段以同样的方式传入，而不会和位置参数撞上。
    """
    body: dict[str, object] = {"name": "topic", "publish_mode": "MANUAL"}
    body.update(fields)
    return body


def _audit(session: Session, action: str, result: str | None = None) -> list[AuditLog]:
    """按插入顺序返回某个动作的审计行，可选只取某种结果。"""
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    if result is not None:
        statement = statement.where(AuditLog.result == result)
    return list(session.scalars(statement).all())


def _create(client: TestClient, headers: dict[str, str], name: str, **overrides: object) -> int:
    """通过 API 创建一个主题并返回它的 id；没创建成功就直接失败。"""
    response = client.post(TOPICS, json=_payload(name=name, **overrides), headers=headers)
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


# --------------------------------------------------------------------------------------
# 认证与角色矩阵
# --------------------------------------------------------------------------------------


def test_the_list_refuses_an_anonymous_caller(client: TestClient) -> None:
    """证明读主题不是公开能力：它需要账号，而且拒绝时要说明是哪一种问题。"""
    response = client.get(TOPICS)
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"


@pytest.mark.parametrize("role", READING_ROLES)
def test_every_standard_role_may_read_the_list(
    client: TestClient,
    session: Session,
    role: str,
) -> None:
    """证明读侧不是特权：这些主题所切分的目录本身就是可读的。"""
    actor = create_actor(session, username=f"route-reader-{role}", role=role)
    response = client.get(TOPICS, headers=auth_headers(client, actor))
    assert response.status_code == 200, response.text


def test_the_write_endpoints_refuse_a_viewer_and_an_editor(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    viewer: Actor,
    editor: Actor,
) -> None:
    """证明 `EDITOR` 的写权限只覆盖报告，且每次拒绝都留了审计。

    两种角色的三个动词都放在同一个用例里检查，因为要点就是这个决定覆盖整组：
    后来新增的写路由若漏了 `ADMIN` 依赖，会在这里失败，而不是在生产上失败。
    """
    topic = workspace.add_topic(name="seeded")
    session.commit()

    for actor in (viewer, editor):
        headers = auth_headers(client, actor)
        assert (
            client.post(TOPICS, json=_payload(name="refused"), headers=headers).status_code == 403
        )
        assert (
            client.patch(
                f"{TOPICS}/{topic.id}", json={"enabled": False}, headers=headers
            ).status_code
            == 403
        )
        assert client.delete(f"{TOPICS}/{topic.id}", headers=headers).status_code == 403

    denials = _audit(session, "auth.rbac")
    assert len(denials) == 6
    assert {row.result for row in denials} == {"denied"}
    assert {row.actor_id for row in denials} == {viewer.user_id, editor.user_id}
    # 拒绝是按请求发生的：调用方在主题被加载之前就被挡回去了，
    # 所以没有资源可点名，编一个出来等于让轨迹谎称发生过一次查询。
    assert {row.resource_id for row in denials} == {None}


# --------------------------------------------------------------------------------------
# 完整的写入路径
# --------------------------------------------------------------------------------------


def test_an_admin_creates_reads_updates_and_deletes(
    client: TestClient,
    session: Session,
    admin: Actor,
) -> None:
    """走一遍完整生命周期，并核对契约给出的响应形态。"""
    headers = auth_headers(client, admin)
    topic_id = _create(
        client,
        headers,
        "created-by-admin",
        description="关注 ANN 索引与检索质量",
        languages=["Rust", "C++"],
        keywords=["vector", "ann"],
        min_stars=200,
        window_days=14,
        max_candidates=20,
    )

    created = client.get(f"{TOPICS}/{topic_id}", headers=headers)
    assert created.status_code == 200
    body = created.json()
    assert body["created_by"] == admin.user_id
    assert body["name"] == "created-by-admin"
    assert body["languages"] == ["Rust", "C++"]
    assert body["keywords"] == ["vector", "ann"]
    assert body["min_stars"] == 200
    assert body["window_days"] == 14
    assert body["max_candidates"] == 20
    assert body["publish_mode"] == "MANUAL"
    assert body["enabled"] is True

    patched = client.patch(
        f"{TOPICS}/{topic_id}",
        json={"enabled": False, "min_stars": 500},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert patched.json()["min_stars"] == 500
    assert patched.json()["languages"] == ["Rust", "C++"]

    deleted = client.delete(f"{TOPICS}/{topic_id}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json() == {
        "topic_id": topic_id,
        "deleted": True,
        "unlinked_candidate_count": 0,
    }

    assert client.get(f"{TOPICS}/{topic_id}", headers=headers).status_code == 404

    # 每个动词一条 `success`，按顺序：轨迹记录的是整个生命周期，不只是最后一步。
    assert [row.result for row in _audit(session, "topic.create")] == ["success"]
    assert [row.result for row in _audit(session, "topic.update")] == ["success"]
    assert [row.result for row in _audit(session, "topic.delete")] == ["success"]


def test_the_list_element_and_the_single_read_have_the_same_shape(
    client: TestClient,
    admin: Actor,
) -> None:
    """契约说详情响应就是列表元素；这里把它断言出来。"""
    headers = auth_headers(client, admin)
    topic_id = _create(client, headers, "shaped", description="d")

    listed = client.get(TOPICS, headers=headers).json()["items"]
    detail = client.get(f"{TOPICS}/{topic_id}", headers=headers).json()

    assert len(listed) == 1
    assert set(listed[0]) == set(detail)
    for field in ("id", "name", "description", "enabled", "publish_mode", "created_by"):
        assert listed[0][field] == detail[field]


def test_every_write_records_the_request_that_caused_it(
    client: TestClient,
    session: Session,
    admin: Actor,
) -> None:
    """证明每条 `topic.*` 审计行都带着中间件分配的请求关联 id。

    这是真实运行时发现的缺陷。服务自己写审计行，却没带上这个 id，
    于是一次主题变更能关联到操作者，却关联不到记录他们发了什么的请求日志 ——
    而那个 id 存在的唯一意义正在于此。项目里其他由路由产生的行都带着它
    （`api/routes/auth.py`、`web/router.py`），正因为如此才看得出这里漏了。

    三个动词放在一起断言，是因为这个 id 要穿过三个不同的函数，
    只修好其中一个，从外面看没有任何区别。
    """
    headers = {**auth_headers(client, admin), "X-Request-ID": "drill-0001"}
    topic_id = _create(client, headers, "correlated")
    assert (
        client.patch(f"{TOPICS}/{topic_id}", json={"enabled": False}, headers=headers).status_code
        == 200
    )
    assert client.delete(f"{TOPICS}/{topic_id}", headers=headers).status_code == 200

    rows = session.scalars(
        select(AuditLog).where(
            AuditLog.action.in_(("topic.create", "topic.update", "topic.delete"))
        )
    ).all()
    assert len(rows) == 3
    assert {row.request_id for row in rows} == {"drill-0001"}


def test_a_refused_write_records_the_request_too(
    client: TestClient,
    session: Session,
    admin: Actor,
) -> None:
    """证明被拒的那一行同样带着 id，因为它恰恰是最值得关联的一行。

    这条用例与上面那条分开写，是因为拒绝走的是 `_refuse` 而不是成功分支：
    只覆盖顺利路径的修法能通过上面那条，却会在这里失败。
    """
    headers = {**auth_headers(client, admin), "X-Request-ID": "drill-0002"}
    _create(client, headers, "taken-name")
    response = client.post(TOPICS, json=_payload(name="taken-name"), headers=headers)

    assert response.status_code == 409
    failure = session.scalar(
        select(AuditLog).where(
            AuditLog.action == "topic.create",
            AuditLog.result == "failed",
        )
    )
    assert failure is not None
    assert failure.request_id == "drill-0002"
    assert failure.metadata_json is not None
    assert failure.metadata_json["reason"] == "TOPIC_NAME_CONFLICT"


# --------------------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------------------


def test_a_blank_name_is_refused_at_the_boundary_with_the_field_named(
    client: TestClient,
    admin: Actor,
) -> None:
    """证明 `422` 既点出字段又说明原因，否则调用方无从下手。"""
    response = client.post(TOPICS, json=_payload(name="   "), headers=auth_headers(client, admin))
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "name" in body["detail"]
    assert "blank" in body["detail"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"publish_mode": "SOMETIMES"},
        {"min_stars": -1},
        {"window_days": 0},
        {"max_candidates": 0},
        {"name": "x" * 129},
        {"languages": ["y" * 65]},
    ],
)
def test_a_field_outside_its_documented_bounds_is_a_validation_error(
    client: TestClient,
    admin: Actor,
    overrides: dict[str, object],
) -> None:
    """每一行对应契约校验表里的一条。

    `name` 那一行与其他行走同一次合并，所以覆盖必填字段的那个用例
    验的是请求模型本身，而不是辅助函数。
    """
    response = client.post(TOPICS, json=_payload(**overrides), headers=auth_headers(client, admin))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}])
def test_pagination_outside_its_bounds_is_a_validation_error(
    client: TestClient,
    admin: Actor,
    params: dict[str, int],
) -> None:
    """证明边界由查询参数声明强制执行，所以是 `422`，而不是被截断的一页。"""
    response = client.get(TOPICS, params=params, headers=auth_headers(client, admin))
    assert response.status_code == 422


def test_a_duplicate_name_is_a_conflict(client: TestClient, admin: Actor) -> None:
    headers = auth_headers(client, admin)
    _create(client, headers, "duplicate")
    response = client.post(TOPICS, json=_payload(name="duplicate"), headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "TOPIC_NAME_CONFLICT"


def test_a_name_that_only_differs_by_whitespace_is_the_same_name(
    client: TestClient,
    admin: Actor,
) -> None:
    """库里两行说的是同一件事、唯一性检查却只看到一个，正是要避免的失败形态。"""
    headers = auth_headers(client, admin)
    _create(client, headers, "向量数据库")
    response = client.post(TOPICS, json=_payload(name=" 向量数据库 "), headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "TOPIC_NAME_CONFLICT"


def test_renaming_onto_a_taken_name_is_a_conflict_and_changes_nothing(
    client: TestClient,
    session: Session,
    admin: Actor,
) -> None:
    headers = auth_headers(client, admin)
    _create(client, headers, "rename-first")
    second = _create(client, headers, "rename-second", min_stars=5)

    response = client.patch(
        f"{TOPICS}/{second}",
        json={"name": "rename-first", "min_stars": 999},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "TOPIC_NAME_CONFLICT"
    session.expire_all()
    reloaded = session.get(Topic, second)
    assert reloaded is not None
    assert reloaded.name == "rename-second"
    assert reloaded.min_stars == 5


def test_a_patch_that_changes_nothing_is_still_a_success(client: TestClient, admin: Actor) -> None:
    headers = auth_headers(client, admin)
    topic_id = _create(client, headers, "idempotent")
    response = client.patch(f"{TOPICS}/{topic_id}", json={"name": "idempotent"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["name"] == "idempotent"


# --------------------------------------------------------------------------------------
# 可见性
# --------------------------------------------------------------------------------------


@pytest.fixture()
def mixed(session: Session, workspace: Workspace) -> dict[str, int]:
    """一个启用主题和一个停用主题，外加断言要比对的 id。"""
    enabled = workspace.add_topic(name="visible-topic", enabled=True)
    disabled = workspace.add_topic(name="hidden-topic", enabled=False)
    session.commit()
    return {"enabled": enabled.id, "disabled": disabled.id}


def test_a_disabled_topic_is_absent_from_a_viewers_items_and_total(
    client: TestClient,
    viewer: Actor,
    mixed: dict[str, int],
) -> None:
    """证明过滤落到了数据库上，所以计数里不可能算进调用方看不到的主题。"""
    body = client.get(TOPICS, headers=auth_headers(client, viewer)).json()
    assert [item["id"] for item in body["items"]] == [mixed["enabled"]]
    assert body["total"] == 1


def test_an_admin_reads_the_disabled_topic_and_the_total_counts_it(
    client: TestClient,
    admin: Actor,
    mixed: dict[str, int],
) -> None:
    body = client.get(TOPICS, headers=auth_headers(client, admin)).json()
    assert {item["id"] for item in body["items"]} == set(mixed.values())
    assert body["total"] == 2


def test_a_disabled_topic_is_forbidden_not_missing(
    client: TestClient,
    viewer: Actor,
    mixed: dict[str, int],
) -> None:
    """证明存在但不可见的主题是 `403`；契约把它与 `404` 分开。"""
    response = client.get(f"{TOPICS}/{mixed['disabled']}", headers=auth_headers(client, viewer))
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_the_denial_says_nothing_about_the_topic_and_the_audit_row_names_it(
    client: TestClient,
    session: Session,
    viewer: Actor,
    mixed: dict[str, int],
) -> None:
    """一次拒绝的两面放在一起断言，因为单看任何一面都能被单独满足。

    响应体泄露名字就是信息泄露；审计行漏掉 id 就无从追查。
    两者都不会表现为状态码错误，所以这里都钉住。
    """
    response = client.get(f"{TOPICS}/{mixed['disabled']}", headers=auth_headers(client, viewer))

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "hidden-topic" not in detail
    assert str(mixed["disabled"]) not in detail

    rows = _audit(session, "topic.read", "denied")
    assert len(rows) == 1
    assert rows[0].resource_type == "topic"
    assert rows[0].resource_id == str(mixed["disabled"])
    assert rows[0].actor_id == viewer.user_id


def test_a_permitted_read_writes_no_audit_row(
    client: TestClient,
    session: Session,
    viewer: Actor,
    mixed: dict[str, int],
) -> None:
    """轨迹记的是拒绝，不是流量。"""
    response = client.get(f"{TOPICS}/{mixed['enabled']}", headers=auth_headers(client, viewer))
    assert response.status_code == 200
    assert _audit(session, "topic.read") == []


def test_the_enabled_filter_intersects_visibility_instead_of_replacing_it(
    client: TestClient,
    viewer: Actor,
    admin: Actor,
    mixed: dict[str, int],
) -> None:
    """`enabled=false` 对只读者是一个真答案，对管理员则是一份列表。

    「你看不到任何停用的主题」本身就回答了被问的问题，
    所以该返回空页而不是 `403`。把它报成拒绝，
    等于告诉调用方有些东西存在，但它无权知道。
    """
    viewer_body = client.get(f"{TOPICS}?enabled=false", headers=auth_headers(client, viewer)).json()
    assert viewer_body == {"items": [], "page": 1, "page_size": 20, "total": 0}

    admin_body = client.get(f"{TOPICS}?enabled=false", headers=auth_headers(client, admin)).json()
    assert [item["id"] for item in admin_body["items"]] == [mixed["disabled"]]
    assert admin_body["total"] == 1


# --------------------------------------------------------------------------------------
# 查询参数
# --------------------------------------------------------------------------------------


@pytest.fixture()
def searched(session: Session, workspace: Workspace) -> None:
    """挑三个名字，让每个元字符用例都恰好只有一个正确答案。"""
    workspace.add_topic(name="vector_db")
    # 只有当 `_` 被当成通配符时才会命中 `vector_db`，而这正是要排除的失败：
    # 搜 `_` 这个子串，不能匹配到一个不含它的名字。
    workspace.add_topic(name="vectorXdb")
    workspace.add_topic(name="covers 65% of cases")
    session.commit()


def _total(client: TestClient, headers: dict[str, str], query: str) -> int:
    """返回某个 `q` 对应的 `total`，读计数而不是读条目。

    计数才是要紧的断言：`items` 可以看起来对，而 `total` 是按
    另一套谓词算出来的；只有两者出自同一个条件列表时它们才一致 ——
    这正是 `queries.py` 文档所承诺的性质。
    """
    body = client.get(TOPICS, params={"q": query}, headers=headers).json()
    return int(body["total"])


def test_the_query_is_a_literal_substring_search(
    client: TestClient,
    admin: Actor,
    searched: None,
) -> None:
    """证明 `%` 与 `_` 对调用方是字符，对数据库也必须只是字符。"""
    headers = auth_headers(client, admin)
    assert _total(client, headers, "vector") == 2
    assert _total(client, headers, "_") == 1
    assert _total(client, headers, "%") == 1
    assert _total(client, headers, "65%") == 1
    assert _total(client, headers, "65_") == 0
    assert _total(client, headers, "zzzznomatch") == 0


def test_the_query_is_case_insensitive(client: TestClient, admin: Actor, searched: None) -> None:
    headers = auth_headers(client, admin)
    assert _total(client, headers, "VECTOR") == 2


def test_the_query_searches_the_description_too(client: TestClient, admin: Actor) -> None:
    headers = auth_headers(client, admin)
    _create(client, headers, "described", description="attention is all you need")
    body = client.get(TOPICS, params={"q": "attention"}, headers=headers).json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "described"


# --------------------------------------------------------------------------------------
# 分页与排序
# --------------------------------------------------------------------------------------


def test_the_list_orders_by_id_and_pages_without_gaps(client: TestClient, admin: Actor) -> None:
    """`id` 升序正是每日流水线读取启用主题所用的顺序。"""
    headers = auth_headers(client, admin)
    created = [_create(client, headers, f"ordered-{index}") for index in range(3)]

    first = client.get(TOPICS, params={"page": 1, "page_size": 2}, headers=headers).json()
    second = client.get(TOPICS, params={"page": 2, "page_size": 2}, headers=headers).json()

    assert first["total"] == 3
    assert [item["id"] for item in first["items"]] == created[:2]
    assert [item["id"] for item in second["items"]] == created[2:]


# --------------------------------------------------------------------------------------
# 删除，以及比主题活得更久的那些资源
# --------------------------------------------------------------------------------------


def test_deleting_a_topic_that_recorded_history_is_a_conflict(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    admin: Actor,
) -> None:
    """拒绝时会点名相关的表，调用方因此知道该去看哪一条关系。"""
    topic = workspace.add_topic(name="with-history")
    workspace.add_agent_run(topic.id)
    session.commit()

    response = client.delete(f"{TOPICS}/{topic.id}", headers=auth_headers(client, admin))

    assert response.status_code == 409
    assert response.json()["code"] == "TOPIC_IN_USE"
    assert "agent_runs" in response.json()["detail"]


@pytest.mark.parametrize("verbs", ["patch", "delete"])
def test_a_missing_topic_is_a_not_found(
    client: TestClient,
    admin: Actor,
    verbs: str,
) -> None:
    headers = auth_headers(client, admin)
    if verbs == "patch":
        response = client.patch(f"{TOPICS}/987654", json={"enabled": False}, headers=headers)
    else:
        response = client.delete(f"{TOPICS}/987654", headers=headers)
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_triggering_a_research_run_is_not_reachable(
    client: TestClient,
    admin: Actor,
) -> None:
    """§3.2 把运行触发挡在 API 之外；这条路由不能自己冒出来。

    一次运行要花模型预算，触发入口归调度器与运维 CLI。
    这里的断言是根本没有路由应答该路径，
    这同时也能拦住将来有人顺手加上一条、却没重新审视这个决定的改动。
    """
    headers = auth_headers(client, admin)
    topic_id = _create(client, headers, "no-run-trigger")
    assert client.post(f"{TOPICS}/{topic_id}/run", headers=headers).status_code == 404
