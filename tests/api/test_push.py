"""守护推送 API：投递白名单、单次投递、投递流水与重试扫描的完整链路。

用例用 HTTP 跑完 T13 这条链路，并覆盖守着它的拒绝映射与访问控制。全程使用默认的
`push_delivery="offline"` 传输，走的是真实路由、服务与持久化层，却不碰网络。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from api_support import Actor, audit_trail, auth_headers, build_client, create_actor
from fastapi.testclient import TestClient
from notifications_support import APPROVED, PUBLISHED, seed_pushable_report
from sqlalchemy.orm import Session, sessionmaker
from support import NOW, Workspace, build_settings

from open_source_intelligence.notifications import service as notifications_service
from open_source_intelligence.persistence.models.notifications import ReportPush

BASE = "/api/v1/reports"
PUSH = "/api/v1/push"


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """管理员：既能读全部报告，也能推送。"""
    return create_actor(session, username="push-admin", role="ADMIN")


@pytest.fixture()
def editor(session: Session) -> Actor:
    """团队数据范围上的编辑，持有另一个可写角色。"""
    return create_actor(session, username="push-editor", role="EDITOR", data_scope="team")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """只读用户：完全不该有推送能力。"""
    return create_actor(session, username="push-viewer", role="VIEWER")


def _seed_failed_push(
    session: Session,
    *,
    report_id: int,
    target_id: int,
    version: int = 1,
    attempt: int = 1,
) -> ReportPush:
    """埋一行失败的投递，顶替一个上次拒绝了的传输通道。"""
    row = ReportPush(
        report_id=report_id,
        target_id=target_id,
        version=version,
        channel="email",
        destination="ops@example.com",
        status="failed",
        attempt=attempt,
        idempotency_key=ReportPush.key_for(
            report_id=report_id,
            version=version,
            target_id=target_id,
        ),
        error_code="PUSH_DELIVERY_FAILED",
        error_message="smtp:transient",
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(row)
    session.commit()
    return row


# --------------------------------------------------------------------------------------
# 白名单
# --------------------------------------------------------------------------------------


def test_the_allowlist_lists_enabled_targets_only(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """被停用的目标不会出现在运维者可选的清单里。"""
    live = workspace.add_push_target(name="ops-email")
    workspace.add_push_target(name="retired-email", destination="old@example.com", enabled=False)

    response = client.get(f"{PUSH}/targets", headers=auth_headers(client, admin))

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == [live.id]
    assert body["items"][0]["channel"] == "email"
    assert body["items"][0]["destination"] == "ops@example.com"
    assert body["items"][0]["allow_private"] is False


def test_the_allowlist_can_be_filtered_by_channel(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """渠道筛选会收窄清单，而没人使用的渠道则返回空。"""
    workspace.add_push_target(name="ops-email")
    hooks = workspace.add_push_target(
        name="ops-webhook",
        channel="webhook",
        destination="https://hooks.example.com/osia",
    )

    headers = auth_headers(client, admin)
    webhooks = client.get(f"{PUSH}/targets", params={"channel": "webhook"}, headers=headers)
    other = client.get(f"{PUSH}/targets", params={"channel": "sms"}, headers=headers)

    assert [item["id"] for item in webhooks.json()["items"]] == [hooks.id]
    assert other.json()["items"] == []


# --------------------------------------------------------------------------------------
# 投递
# --------------------------------------------------------------------------------------


def test_a_delivery_is_recorded_and_visible_in_the_trail(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """一次推送写下一条 `sent` 记录和一条审计，流水里的计数也如实反映。"""
    report = seed_pushable_report(session, workspace, status=APPROVED)
    target = workspace.add_push_target()

    response = client.post(
        f"{BASE}/{report.id}/push",
        json={"channel": "email", "target_id": target.id},
        headers=auth_headers(client, admin),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report_id"] == report.id
    assert body["version"] == report.current_version
    assert body["channel"] == "email"
    assert body["destination"] == "ops@example.com"
    assert body["status"] == "sent"
    assert body["attempt"] == 1
    assert body["delivered"] is True
    assert body["replayed"] is False

    trail = client.get(
        f"{PUSH}/records",
        params={"report_id": report.id},
        headers=auth_headers(client, admin),
    )
    assert trail.status_code == 200, trail.text
    records = trail.json()
    assert [item["id"] for item in records["items"]] == [body["push_id"]]
    assert records["status_counts"] == {"sent": 1}
    assert records["items"][0]["sent_at"] is not None

    assert ("report.push", "success") in audit_trail(session_factory)


def test_a_repeat_request_replays_instead_of_sending_twice(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """同一个（报告、版本、目标）三元组最多只投递一次。"""
    report = seed_pushable_report(session, workspace, status=APPROVED)
    target = workspace.add_push_target()
    headers = auth_headers(client, admin)
    payload = {"channel": "email", "target_id": target.id}

    first = client.post(f"{BASE}/{report.id}/push", json=payload, headers=headers)
    second = client.post(f"{BASE}/{report.id}/push", json=payload, headers=headers)

    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert second.json()["push_id"] == first.json()["push_id"]
    assert second.json()["attempt"] == 1

    trail = client.get(f"{PUSH}/records", params={"report_id": report.id}, headers=headers)
    assert len(trail.json()["items"]) == 1
    # 只有第一次投递进审计：重放根本走不到传输层。
    assert audit_trail(session_factory).count(("report.push", "success")) == 1


def test_a_published_report_can_be_pushed(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """`published` 是另一条允许推送的状态。"""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    target = workspace.add_push_target()

    response = client.post(
        f"{BASE}/{report.id}/push",
        json={"channel": "email", "target_id": target.id},
        headers=auth_headers(client, admin),
    )

    assert response.status_code == 200, response.text
    assert response.json()["delivered"] is True


def test_a_report_that_is_not_approved_is_refused_with_409(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """只有通过审核或已发布的报告才允许出门。"""
    report = seed_pushable_report(session, workspace, status="draft")
    target = workspace.add_push_target()

    response = client.post(
        f"{BASE}/{report.id}/push",
        json={"channel": "email", "target_id": target.id},
        headers=auth_headers(client, admin),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "PUSH_BLOCKED"


def test_a_confidential_report_without_approval_is_refused_with_409(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """敏感材料必须先有一份留痕的人工批准，才可能被发出去。"""
    report = seed_pushable_report(session, workspace, sensitivity="CONFIDENTIAL")
    target = workspace.add_push_target(allow_confidential=True)

    response = client.post(
        f"{BASE}/{report.id}/push",
        json={"channel": "email", "target_id": target.id},
        headers=auth_headers(client, admin),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "PUSH_APPROVAL_REQUIRED"


def test_a_target_off_the_allowlist_is_refused_with_400(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """未知的目标 id 是请求有问题，而不是投递失败。"""
    report = seed_pushable_report(session, workspace, status=APPROVED)

    response = client.post(
        f"{BASE}/{report.id}/push",
        json={"channel": "email", "target_id": 999_999},
        headers=auth_headers(client, admin),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "PUSH_TARGET_NOT_ALLOWED"


def test_a_channel_that_does_not_match_the_target_is_refused_with_400(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """渠道不能被凭空信任：它必须与白名单那一行对得上。"""
    report = seed_pushable_report(session, workspace, status=APPROVED)
    target = workspace.add_push_target()

    response = client.post(
        f"{BASE}/{report.id}/push",
        json={"channel": "webhook", "target_id": target.id},
        headers=auth_headers(client, admin),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "PUSH_TARGET_NOT_ALLOWED"


def test_a_private_report_needs_a_cleared_target(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """没有为 `PRIVATE` 报告放行过的目标，一律拒绝。"""
    report = seed_pushable_report(session, workspace, status=APPROVED, visibility="PRIVATE")
    target = workspace.add_push_target()

    response = client.post(
        f"{BASE}/{report.id}/push",
        json={"channel": "email", "target_id": target.id},
        headers=auth_headers(client, admin),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "PUSH_TARGET_NOT_ALLOWED"


def test_an_unknown_report_is_refused_with_404(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """报告不存在时答的是 Problem Details，而不是一个内部错误。"""
    target = workspace.add_push_target()

    response = client.post(
        f"{BASE}/999999/push",
        json={"channel": "email", "target_id": target.id},
        headers=auth_headers(client, admin),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_an_unsupported_channel_is_a_validation_error(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """渠道是闭集，所以别的值在到达服务层之前就该失败。"""
    report = seed_pushable_report(session, workspace, status=APPROVED)
    target = workspace.add_push_target()

    response = client.post(
        f"{BASE}/{report.id}/push",
        json={"channel": "sms", "target_id": target.id},
        headers=auth_headers(client, admin),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_pushing_reports_unavailable_when_the_feature_is_off(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关掉这个功能答 `503`，它告诉调用方过一会儿再来。"""
    report = seed_pushable_report(session, workspace, status=APPROVED)
    target = workspace.add_push_target()
    monkeypatch.setattr(
        notifications_service,
        "get_settings",
        lambda: build_settings(push_enabled=False),
    )

    response = client.post(
        f"{BASE}/{report.id}/push",
        json={"channel": "email", "target_id": target.id},
        headers=auth_headers(client, admin),
    )

    assert response.status_code == 503
    assert response.json()["code"] == "PUSH_DISABLED"


# --------------------------------------------------------------------------------------
# 重试扫描
# --------------------------------------------------------------------------------------


def test_the_retry_driver_redelivers_a_failed_push(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """还有预算的失败行会被扫描重新投递，而且仍落在同一行上。"""
    report = seed_pushable_report(session, workspace, status=APPROVED)
    target = workspace.add_push_target()
    failed = _seed_failed_push(session, report_id=report.id, target_id=target.id)

    response = client.post(f"{PUSH}/retry", headers=auth_headers(client, admin))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["attempted_count"] == 1
    assert body["delivered_count"] == 1
    assert body["failed_count"] == 0
    assert body["outcomes"][0]["push_id"] == failed.id
    assert body["outcomes"][0]["status"] == "sent"
    assert body["outcomes"][0]["attempt"] == 2

    trail = client.get(
        f"{PUSH}/records",
        params={"report_id": report.id},
        headers=auth_headers(client, admin),
    )
    assert trail.json()["status_counts"] == {"sent": 1}


def test_the_retry_driver_is_a_no_op_without_failed_rows(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """没有可重试的行，就是一次成功但为空的扫描。"""
    response = client.post(f"{PUSH}/retry", headers=auth_headers(client, admin))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "attempted_count": 0,
        "delivered_count": 0,
        "failed_count": 0,
        "outcomes": [],
    }


def test_the_retry_driver_skips_rows_that_are_out_of_budget(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """已经用完重试预算的行放着不动，而不是被无限重试。"""
    report = seed_pushable_report(session, workspace, status=APPROVED)
    target = workspace.add_push_target()
    _seed_failed_push(session, report_id=report.id, target_id=target.id, attempt=3)

    response = client.post(f"{PUSH}/retry", headers=auth_headers(client, admin))

    assert response.json()["attempted_count"] == 0


def test_records_can_be_filtered_by_status(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """运维者靠状态筛选把积压单独拎出来。"""
    report = seed_pushable_report(session, workspace, status=APPROVED)
    target = workspace.add_push_target()
    failed = _seed_failed_push(session, report_id=report.id, target_id=target.id)

    headers = auth_headers(client, admin)
    only_failed = client.get(f"{PUSH}/records", params={"status": "failed"}, headers=headers)
    only_sent = client.get(f"{PUSH}/records", params={"status": "sent"}, headers=headers)

    assert [item["id"] for item in only_failed.json()["items"]] == [failed.id]
    assert only_failed.json()["items"][0]["error_code"] == "PUSH_DELIVERY_FAILED"
    assert only_sent.json()["items"] == []


# --------------------------------------------------------------------------------------
# 访问控制
# --------------------------------------------------------------------------------------


def test_a_read_only_role_cannot_push(
    session: Session,
    workspace: Workspace,
    viewer: Actor,
    client: TestClient,
) -> None:
    """推送是写动作，因此 `VIEWER` 在任何守卫之前就被拒了。"""
    report = seed_pushable_report(session, workspace, status=APPROVED)
    target = workspace.add_push_target()

    response = client.post(
        f"{BASE}/{report.id}/push",
        json={"channel": "email", "target_id": target.id},
        headers=auth_headers(client, viewer),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_the_push_endpoints_require_authentication(client: TestClient) -> None:
    """没有令牌：推送、白名单、流水、扫描四处都答 `401`。"""
    action = client.post(f"{BASE}/1/push", json={"channel": "email", "target_id": 1})
    assert action.status_code == 401
    assert client.get(f"{PUSH}/targets").status_code == 401
    assert client.get(f"{PUSH}/records").status_code == 401
    assert client.post(f"{PUSH}/retry").status_code == 401


def test_an_editor_can_push_a_report_it_can_read(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """另一个写角色也放行，前提是这份报告本身对它可读。"""
    report = seed_pushable_report(session, workspace, status=APPROVED)
    target = workspace.add_push_target()

    response = client.post(
        f"{BASE}/{report.id}/push",
        json={"channel": "email", "target_id": target.id},
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 200, response.text
    assert response.json()["delivered"] is True


def test_the_delivery_ledger_is_filtered_by_the_report_acl(
    session: Session,
    workspace: Workspace,
    client: TestClient,
) -> None:
    """投递行属于报告元数据，所以要过报告 ACL，光有角色检查不够。

    这一行里写着报告标题、版本、目标和错误文本。只按角色过滤时，编辑能读到自己
    打不开的 `PRIVATE` 报告的全部这些内容 —— 重试扫描甚至会替他重新发一遍。
    """
    owner = create_actor(session, username="push-owner", role="EDITOR", data_scope="team")
    other = create_actor(session, username="push-other", role="EDITOR", data_scope="team")
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    report.visibility = "PRIVATE"
    report.owner_id = owner.user_id
    session.commit()
    _seed_failed_push(
        session,
        report_id=report.id,
        target_id=workspace.add_push_target(name="acl-ledger", allow_private=True).id,
    )

    stranger = client.get(f"{PUSH}/records", headers=auth_headers(client, other))
    assert stranger.status_code == 200, stranger.text
    assert stranger.json()["items"] == []

    insider = client.get(f"{PUSH}/records", headers=auth_headers(client, owner))
    assert insider.status_code == 200, insider.text
    assert [item["report_id"] for item in insider.json()["items"]] == [report.id]
