"""守护管理界面里的推送卡片：同一条链路，换界面这条通道。

用例走的是 T13 界面路径，底层仍是 JSON API 测试用的那套真实路由 / 服务 /
持久化，并检查界面在此之上新增的两件事：动作要过 Cookie 会话加双提交 CSRF
令牌；渠道取自白名单那一行，而不是取自浏览器。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from api_support import (
    Actor,
    audit_trail,
    build_client,
    create_actor,
    csrf_value,
    prime_csrf,
    web_login,
)
from fastapi.testclient import TestClient
from notifications_support import APPROVED, PUBLISHED, seed_pushable_report
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace

from open_source_intelligence.notifications.service import list_pushes

BASE = "/ui/reports"


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """管理员：既能读全部报告，也能推送。"""
    return create_actor(session, username="ui-push-admin", role="ADMIN")


@pytest.fixture()
def editor(session: Session) -> Actor:
    """团队范围上的编辑，持有另一个可写角色。"""
    return create_actor(session, username="ui-push-editor", role="EDITOR", data_scope="team")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """只读用户：完全不该有推送能力。"""
    return create_actor(session, username="ui-push-viewer", role="VIEWER")


def _token(client: TestClient) -> dict[str, str]:
    """返回一个写请求要用的 CSRF 表单载荷。"""
    return {"csrf_token": csrf_value(client)}


def _push_audit(session_factory: sessionmaker[Session]) -> list[tuple[str, str]]:
    """只取推送相关的审计条目，免得写入夹具的杂音混进断言里。"""
    return [entry for entry in audit_trail(session_factory) if entry[0] == "report.push"]


# --------------------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------------------


def test_the_panel_offers_the_allowlist_and_the_delivery_history(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """卡片列出投递提示，以及每一个被放行的目的地。"""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    target = workspace.add_push_target(name="ops-email")
    assert web_login(client, editor).status_code == 303

    page = client.get(f"{BASE}/{report.id}")

    assert page.status_code == 200
    assert f'action="{BASE}/{report.id}/push"' in page.text
    assert "推送记录" in page.text
    assert "只有 approved / published 状态的报告可以推送" in page.text
    assert f"{target.name}（{target.channel} · {target.destination}）" in page.text
    assert "还没有推送记录。" in page.text


def test_the_panel_explains_a_missing_target_for_the_role_reading_it(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    admin: Actor,
    client: TestClient,
) -> None:
    """没有白名单条目时，卡片要用读者能据此行动的措辞解释原因。

    添加推送目标是后端写入的部署配置，编辑做不了。叫编辑去改 `push_targets`，
    描述的是他并不具备的动作 —— 还顺手把一个他本不该去对表的表名递给了他。能动手
    的管理员拿到表名，编辑则拿到「去找管理员」。
    """
    report = seed_pushable_report(session, workspace, status=APPROVED)

    assert web_login(client, editor).status_code == 303
    editor_page = client.get(f"{BASE}/{report.id}")
    assert f'action="{BASE}/{report.id}/push"' not in editor_page.text
    assert "请联系管理员在运维侧添加并启用" in editor_page.text
    assert "push_targets" not in editor_page.text

    assert web_login(client, admin).status_code == 303
    admin_page = client.get(f"{BASE}/{report.id}")
    assert f'action="{BASE}/{report.id}/push"' not in admin_page.text
    assert "push_targets" in admin_page.text
    assert "请联系管理员" not in admin_page.text


# --------------------------------------------------------------------------------------
# 投递
# --------------------------------------------------------------------------------------


def test_pushing_from_the_panel_records_the_delivery(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """表单提交完成投递，刷新后的面板显示出那条已发送记录。"""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    target = workspace.add_push_target()
    assert web_login(client, editor).status_code == 303

    response = client.post(
        f"{BASE}/{report.id}/push",
        data={**_token(client), "target_id": target.id},
    )

    assert response.status_code == 200, response.text
    assert f"已推送到 {target.destination}（第 1 次尝试）" in response.text
    assert 'class="ok-text">已送达</td>' in response.text
    assert ("report.push", "success") in audit_trail(session_factory)


def test_a_second_post_from_the_panel_replays_instead_of_resending(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """同一个目标推两次就是重放，面板也这样说明。"""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    target = workspace.add_push_target()
    assert web_login(client, editor).status_code == 303
    payload = {**_token(client), "target_id": target.id}

    first = client.post(f"{BASE}/{report.id}/push", data=payload)
    second = client.post(f"{BASE}/{report.id}/push", data=payload)

    assert "已推送到" in first.text
    assert "（该版本与目标此前已推送，未重复发送）" in second.text
    assert "第 1 次尝试" in second.text


def test_a_blocked_push_is_rendered_as_a_refusal_not_a_crash(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """草稿出不了门，面板会带着原因回来。"""
    report = seed_pushable_report(session, workspace, status="draft")
    target = workspace.add_push_target()
    assert web_login(client, editor).status_code == 303

    response = client.post(
        f"{BASE}/{report.id}/push",
        data={**_token(client), "target_id": target.id},
    )

    assert response.status_code == 200, response.text
    assert "PUSH_BLOCKED" in response.text
    assert not _push_audit(session_factory)


def test_the_channel_comes_from_the_allowlist_not_the_form(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """被伪造的渠道字段会被忽略；用哪条传输由目标那一行决定。"""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    target = workspace.add_push_target(name="ops-email")
    assert web_login(client, editor).status_code == 303

    response = client.post(
        f"{BASE}/{report.id}/push",
        data={**_token(client), "target_id": target.id, "channel": "webhook"},
    )

    assert response.status_code == 200, response.text
    assert "已推送到 ops@example.com" in response.text
    with session_factory() as check:
        rows = list_pushes(check, report_id=report.id)
    assert [row.channel for row in rows] == ["email"]


def test_an_unknown_target_renders_the_refusal(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """不在白名单上的目标 id 会被拒绝，并给出一个可读的错误码。"""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    workspace.add_push_target()
    assert web_login(client, editor).status_code == 303

    response = client.post(
        f"{BASE}/{report.id}/push",
        data={**_token(client), "target_id": 999_999},
    )

    assert response.status_code == 200, response.text
    assert "PUSH_TARGET_NOT_ALLOWED" in response.text


# --------------------------------------------------------------------------------------
# 守卫
# --------------------------------------------------------------------------------------


def test_pushing_through_the_ui_needs_a_csrf_token(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """缺了令牌，在到达服务层之前就被拦住。"""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    target = workspace.add_push_target()
    assert web_login(client, editor).status_code == 303

    response = client.post(f"{BASE}/{report.id}/push", data={"target_id": target.id})

    assert response.status_code == 403
    assert "CSRF 校验失败" in response.text
    assert not _push_audit(session_factory)


def test_a_read_only_role_cannot_push_through_the_ui(
    session: Session,
    workspace: Workspace,
    viewer: Actor,
    client: TestClient,
) -> None:
    """写角色守卫先作答，推送服务根本轮不到。"""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    target = workspace.add_push_target()
    prime_csrf(client)
    assert web_login(client, viewer).status_code == 303

    response = client.post(
        f"{BASE}/{report.id}/push",
        data={**_token(client), "target_id": target.id},
    )

    assert response.status_code == 403
    assert "只读角色不能执行该操作" in response.text


def test_an_anonymous_post_is_redirected_to_login(
    session: Session,
    workspace: Workspace,
    client: TestClient,
) -> None:
    """没有会话就重定向回登录页，带不带令牌都一样。"""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    token = prime_csrf(client)

    response = client.post(
        f"{BASE}/{report.id}/push",
        data={"csrf_token": token, "target_id": 1},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/login")


# --------------------------------------------------------------------------------------
# 投递流水（T30 回归：目标名不能取决于读者的角色）
# --------------------------------------------------------------------------------------


def test_the_delivery_ledger_names_the_target_for_an_editor(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """编辑看到的是目的地的名字，而不是一块墓碑。

    流水曾经靠查白名单来解析名字，而白名单只有 `ADMIN` 读得到 —— 于是目标
    明明还活着且已启用，编辑的页面上每一行都写着「（目标 #N 已不存在）」。
    现在名字被反规范化，直接存在投递行上。
    """
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    target = workspace.add_push_target(name="ops-mailbox")
    assert web_login(client, editor).status_code == 303
    assert (
        client.post(
            f"{BASE}/{report.id}/push",
            data={**_token(client), "target_id": target.id},
        ).status_code
        == 200
    )

    page = client.get("/ui/pushes")

    assert page.status_code == 200
    assert "ops-mailbox" in page.text
    assert "已不存在" not in page.text
    # 名字存在行上，所以日后白名单条目被改指向别处，它也不会变。
    rows = list_pushes(session, limit=10)
    assert [row.target_name for row in rows] == ["ops-mailbox"]
