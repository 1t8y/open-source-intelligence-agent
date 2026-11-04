"""守护审计日志读取端点：谁能读、筛选器是否如实筛选、哪些字段不出网关。

`REQUIREMENTS.md` 的 FR-D-05 要求提供审计 API，FR-S-02 要求登录、配置变更、
评审与发布都可追溯。这组用例覆盖读侧：只有管理员能读这条轨迹，筛选器选中
它们声称选中的行，另有两个字段被刻意排除在响应之外。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from api_support import Actor, auth_headers, build_client, create_actor
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.security.audit import pack_ip_address
from open_source_intelligence.security.queries import MAX_PAGE_SIZE

AUDIT = "/api/v1/audit-logs"

#: 一个只存在于 `metadata_json` 里的值。它不能进入响应：该字段是自由格式的，
#: 允许放哪些键从未定义过，所以出网路径不该带上它。
UNPUBLISHED_METADATA = "internal-context-that-was-never-contracted"


@dataclass(frozen=True, slots=True)
class Trail:
    """夹具写入的审计行，按产生它们的那次动作来标识。"""

    actor_id: int
    report_read_ids: tuple[int, ...]
    auth_csrf_id: int

    @property
    def seeded_ids(self) -> set[int]:
        """夹具写下的全部 id，好让断言可以忽略这次登录事件。"""
        return {*self.report_read_ids, self.auth_csrf_id}


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """管理员 —— 唯一有权读取审计轨迹的角色。"""
    return create_actor(session, username="admin", role="ADMIN")


@pytest.fixture()
def editor(session: Session) -> Actor:
    """编辑：虽然能写报告，但读不了这条轨迹。"""
    return create_actor(session, username="editor", role="EDITOR")


def _event(
    session: Session,
    *,
    actor_id: int | None,
    action: str,
    result: str,
    resource_type: str = "report",
    resource_id: str | None = None,
    ip_address: str | None = None,
    metadata: dict[str, object] | None = None,
) -> AuditLog:
    """插入一行审计记录，刷出 id 之后返回它。"""
    event = AuditLog(
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        request_id="req-1",
        ip_address=pack_ip_address(ip_address),
        metadata_json=metadata,
    )
    session.add(event)
    session.flush()
    return event


@pytest.fixture()
def trail(session: Session, admin: Actor) -> Trail:
    """写入一小段轨迹：两次被拒的报告读取、一次 CSRF 拒绝。"""
    first = _event(
        session,
        actor_id=admin.user_id,
        action="report.read",
        result="denied",
        resource_id="2",
        ip_address="203.0.113.7",
        metadata={"note": UNPUBLISHED_METADATA},
    )
    second = _event(
        session,
        actor_id=admin.user_id,
        action="report.read",
        result="denied",
        resource_id="4",
        ip_address="2001:db8::1",
    )
    csrf = _event(
        session,
        actor_id=None,
        action="auth.csrf",
        result="denied",
        resource_type="auth",
    )
    session.commit()
    return Trail(
        actor_id=admin.user_id,
        report_read_ids=(first.id, second.id),
        auth_csrf_id=csrf.id,
    )


def test_audit_log_requires_a_session(client: TestClient) -> None:
    """轨迹不是公开数据：它点名了平台上每一个被拒绝的请求。"""
    assert client.get(AUDIT).status_code == 401


def test_an_editor_is_refused_and_the_refusal_is_recorded(
    client: TestClient,
    editor: Actor,
    trail: Trail,
    session: Session,
) -> None:
    """读轨迹本身就是一项特权，而拒绝也和别的事件一样要进审计。

    这里刻意与报告 ACL 保持同一种形状：`403` 加一行 `denied`。校验把那一行从
    应用自己的表里读回来，而不是去读一个计数器。
    """
    response = client.get(AUDIT, headers=auth_headers(client, editor))
    assert response.status_code == 403

    denied = session.scalars(
        select(AuditLog).where(AuditLog.action == "auth.rbac", AuditLog.result == "denied")
    ).all()
    assert [row.actor_id for row in denied] == [editor.user_id]


def test_an_administrator_reads_newest_first(
    client: TestClient,
    admin: Actor,
    trail: Trail,
) -> None:
    """轨迹按 id 倒序，管理员自己的登录也在其中。

    登录出现在里面才是重点：端点读的是应用真实的轨迹，而不是只属于夹具的视图，
    因此给调用方做认证的那个请求本身就是一个事件。所以排序断言看的是夹具行的
    相对顺序，而不是它们在整页里的绝对位置。
    """
    response = client.get(AUDIT, headers=auth_headers(client, admin))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 4
    assert [item["action"] for item in body["items"]][0] == "auth.login"

    seeded = [item["id"] for item in body["items"] if item["id"] in trail.seeded_ids]
    assert seeded == [trail.auth_csrf_id, trail.report_read_ids[1], trail.report_read_ids[0]]


def test_metadata_and_nothing_undefined_is_exported(
    client: TestClient,
    admin: Actor,
    trail: Trail,
) -> None:
    """在允许的键被定义之前，`metadata_json` 不出进程。

    断言打在原始响应体上而不是解析后的键上：将来有人把这个字段加上、却没想清楚
    里面能放什么，就会在这里失败，而不是悄悄上线。
    """
    response = client.get(AUDIT, headers=auth_headers(client, admin))
    assert response.status_code == 200
    assert UNPUBLISHED_METADATA not in response.text
    assert "metadata_json" not in response.text
    for item in response.json()["items"]:
        assert "metadata" not in item


def test_packed_addresses_come_back_as_text(
    client: TestClient,
    admin: Actor,
    trail: Trail,
) -> None:
    """同一列里既存 IPv4 也存 IPv6，靠那一对辅助函数才能把两者都读回来。"""
    response = client.get(AUDIT, headers=auth_headers(client, admin))
    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["items"]}
    assert by_id[trail.report_read_ids[0]]["ip_address"] == "203.0.113.7"
    assert by_id[trail.report_read_ids[1]]["ip_address"] == "2001:db8::1"
    # 请求级拒绝没有客户端地址，`null` 才是诚实的答案。
    assert by_id[trail.auth_csrf_id]["ip_address"] is None


def test_filters_select_what_they_say(
    client: TestClient,
    admin: Actor,
    trail: Trail,
) -> None:
    """每个筛选器都同时收窄当页内容与总数。"""
    headers = auth_headers(client, admin)

    by_action = client.get(AUDIT, headers=headers, params={"action": "auth.csrf"}).json()
    assert by_action["total"] == 1
    assert by_action["items"][0]["resource_type"] == "auth"

    by_resource = client.get(
        AUDIT,
        headers=headers,
        params={"resource_type": "report", "resource_id": "4"},
    ).json()
    assert by_resource["total"] == 1
    assert by_resource["items"][0]["id"] == trail.report_read_ids[1]

    by_result = client.get(AUDIT, headers=headers, params={"result": "denied"}).json()
    assert {item["id"] for item in by_result["items"]} == trail.seeded_ids

    # 调用方自己的登录是一条 `success` 事件 —— 正是它让 `result=success` 非空，
    # 而不是一个默默匹配不到任何行、却仍然合法的筛选器。
    succeeded = client.get(AUDIT, headers=headers, params={"result": "success"}).json()
    assert [item["action"] for item in succeeded["items"]] == ["auth.login"]

    by_actor = client.get(AUDIT, headers=headers, params={"actor_id": trail.actor_id}).json()
    assert {item["id"] for item in by_actor["items"]} == {
        *trail.report_read_ids,
        succeeded["items"][0]["id"],
    }


def test_an_unknown_result_is_refused_rather_than_returning_nothing(
    client: TestClient,
    admin: Actor,
    trail: Trail,
) -> None:
    """「没有命中」与「这种结果根本不可能存在」是两种不同的结论。"""
    response = client.get(AUDIT, headers=auth_headers(client, admin), params={"result": "maybe"})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_the_time_window_is_half_open(
    client: TestClient,
    admin: Actor,
    trail: Trail,
) -> None:
    """`created_before` 是排他的，于是一行永远不会同时落在两个相邻窗口里。"""
    headers = auth_headers(client, admin)
    everything = client.get(AUDIT, headers=headers).json()
    newest = next(item for item in everything["items"] if item["id"] == trail.auth_csrf_id)

    # 正好停在最新一行的边界上会把它排除掉：这就是「半开」换来的性质，也是调用方
    # 沿时间向后翻页时所依赖的东西。
    excludes = client.get(
        AUDIT,
        headers=headers,
        params={"created_before": newest["created_at"]},
    ).json()
    assert trail.auth_csrf_id not in {item["id"] for item in excludes["items"]}

    # 朴素时间戳在边界处就被拒绝，而不会一路传到查询构造器里。
    naive = client.get(AUDIT, headers=headers, params={"created_after": "2026-10-15T09:00:00"})
    assert naive.status_code == 422
    assert "timezone offset" in naive.text


def test_page_size_is_bounded(
    client: TestClient,
    admin: Actor,
    trail: Trail,
) -> None:
    """平台统一的 1–100 页长上限同样约束这条轨迹。"""
    response = client.get(
        AUDIT,
        headers=auth_headers(client, admin),
        params={"page_size": MAX_PAGE_SIZE + 1},
    )
    assert response.status_code == 422
