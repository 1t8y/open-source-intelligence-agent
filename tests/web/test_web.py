"""守护服务端渲染的管理界面：同一条验收路径，换一条渲染通道。

用例走的是与 JSON API 测试相同的验收路径 —— 列表、读取、编辑、校验、评审、发布、
回滚 —— 但经过 Jinja2 / HTMX 页面，并检查界面在此之上新增的两道守卫：Cookie 会话
与双提交 CSRF 令牌。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from api_support import (
    Actor,
    audit_trail,
    build_client,
    configure_report,
    create_actor,
    csrf_value,
    load_report,
    prime_csrf,
    web_login,
    web_logout,
)
from fastapi.testclient import TestClient
from reports_support import (
    AUTO_WITH_DELAY,
    MANUAL,
    SeededReport,
    build_report,
    seed_published_sibling,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import NOW, Workspace

from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.reports import queries
from open_source_intelligence.web import router as web_router
from open_source_intelligence.web.security import SESSION_COOKIE

BASE = "/ui/reports"


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """数据范围为「个人」的管理员。"""
    return create_actor(session, username="admin", role="ADMIN")


@pytest.fixture()
def editor(session: Session) -> Actor:
    """团队数据范围上的编辑，能读写团队报告。"""
    return create_actor(session, username="editor", role="EDITOR", data_scope="team")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """数据范围为「个人」的只读用户。"""
    return create_actor(session, username="viewer", role="VIEWER")


def _seed(
    session: Session,
    workspace: Workspace,
    *,
    publish_mode: str = AUTO_WITH_DELAY,
    visibility: str = "TEAM",
    prior_publication: bool = False,
) -> SeededReport:
    """造一份报告，必要时再给它的主题补一份更早的已发布报告。"""
    case = build_report(session, workspace, publish_mode=publish_mode)
    configure_report(session, report_id=case.report_id, visibility=visibility)
    if prior_publication:
        seed_published_sibling(session, case)
    return case


def _token(client: TestClient) -> dict[str, str]:
    """返回一个写请求要用的 CSRF 表单载荷。"""
    return {"csrf_token": csrf_value(client)}


def _detail(session_factory: sessionmaker[Session], report_id: int) -> queries.ReportDetail:
    """用一个短命会话读出聚合后的报告详情。"""
    with session_factory() as session:
        detail = queries.get_report_detail(session, report_id=report_id)
    assert detail is not None
    return detail


# --------------------------------------------------------------------------------------
# 会话与 CSRF
# --------------------------------------------------------------------------------------


def test_anonymous_browsing_redirects_to_login(client: TestClient) -> None:
    """每一页都要会话，并且记得浏览器本来想去哪。"""
    listing = client.get(BASE, follow_redirects=False)
    assert listing.status_code == 303
    assert listing.headers["location"] == "/ui/login?next=%2Fui%2Freports"

    detail = client.get(f"{BASE}/5", follow_redirects=False)
    assert detail.headers["location"] == "/ui/login?next=%2Fui%2Freports%2F5"


def test_a_tampered_session_cookie_redirects_to_login(client: TestClient) -> None:
    """读不出来的 Cookie 与根本没有 Cookie 受到完全相同的对待。"""
    client.cookies.set(SESSION_COOKIE, "not-a-jwt")
    response = client.get(BASE, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/login")


def test_login_page_serves_a_csrf_token(client: TestClient) -> None:
    """表单里带一个隐藏令牌，它与双提交 Cookie 里的值一致。"""
    response = client.get("/ui/login")
    assert response.status_code == 200
    token = csrf_value(client)
    assert 'name="csrf_token"' in response.text
    assert token in response.text


def test_login_stores_an_httponly_session_cookie(client: TestClient, editor: Actor) -> None:
    """登录成功会重定向，并把 JWT 放在 JavaScript 够不到的地方。"""
    response = web_login(client, editor)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/reports"
    cookie_header = response.headers["set-cookie"]
    assert SESSION_COOKIE in cookie_header
    assert "HttpOnly" in cookie_header

    page = client.get(BASE)
    assert page.status_code == 200
    assert "报告中心" in page.text
    assert editor.username in page.text


def test_failed_login_is_audited(
    client: TestClient,
    editor: Actor,
    session_factory: sessionmaker[Session],
) -> None:
    """密码错误会重新渲染表单，并把这次尝试记下来。"""
    token = prime_csrf(client)
    response = client.post(
        "/ui/login",
        data={
            "csrf_token": token,
            "username": editor.username,
            "password": "wrong-password",
            "next": "/ui/reports",
        },
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "用户名或密码不正确" in response.text
    assert not client.cookies.get(SESSION_COOKIE)
    assert ("auth.login", "failed") in audit_trail(session_factory)


def test_logout_clears_the_session(client: TestClient, editor: Actor) -> None:
    """登出会丢掉 Cookie，于是下一页又会重定向。"""
    assert web_login(client, editor).status_code == 303

    response = web_logout(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    assert client.get(BASE, follow_redirects=False).status_code == 303


def test_mutating_actions_reject_a_missing_csrf_token(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """缺了令牌会走到 CSRF 闸门并渲染 403 页面，而不是一个 JSON 错误。"""
    case = _seed(session, workspace)
    assert web_login(client, editor).status_code == 303

    response = client.post(f"{BASE}/{case.report_id}/verify", data={})

    assert response.status_code == 403
    assert "CSRF 校验失败" in response.text


def test_mutating_actions_reject_a_mismatched_csrf_token(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """与 Cookie 对不上的令牌，在任何服务被调用之前就被拒绝。"""
    case = _seed(session, workspace)
    assert web_login(client, editor).status_code == 303

    response = client.post(
        f"{BASE}/{case.report_id}/verify",
        data={"csrf_token": "not-the-cookie"},
    )

    assert response.status_code == 403
    assert "CSRF 校验失败" in response.text


def test_refusal_inside_an_htmx_swap_is_one_line(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """被拒绝的局部替换只回一句提示，而不回 403 页面：那页面会套进容器。"""
    case = _seed(session, workspace)
    assert web_login(client, editor).status_code == 303

    response = client.post(
        f"{BASE}/{case.report_id}/verify",
        data={},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 403
    assert "CSRF 校验失败" in response.text
    assert "<!DOCTYPE" not in response.text
    assert 'class="sidebar"' not in response.text


def test_static_assets_are_served_locally(client: TestClient) -> None:
    """HTMX 随包内置，因此界面在没有外网出口的环境里也能用。"""
    response = client.get("/ui/static/htmx.min.js")
    assert response.status_code == 200
    assert "htmx" in response.text


# --------------------------------------------------------------------------------------
# 页面上的访问控制
# --------------------------------------------------------------------------------------


def test_report_list_hides_reports_outside_the_scope(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    viewer: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """列表在查询里就被过滤，所以读不到的标题根本到不了页面上。"""
    internal = _seed(session, workspace, visibility="INTERNAL")
    private = _seed(session, workspace, visibility="PRIVATE")
    configure_report(session, report_id=private.report_id, owner_id=editor.user_id)
    private_title = load_report(session_factory, private.report_id).title

    assert web_login(client, viewer).status_code == 303
    viewer_page = client.get(BASE)
    assert viewer_page.status_code == 200
    assert f"/ui/reports/{internal.report_id}" in viewer_page.text
    assert private_title not in viewer_page.text

    web_logout(client)
    assert web_login(client, editor).status_code == 303
    assert private_title in client.get(BASE).text


def test_report_list_shows_an_empty_state(
    session: Session,
    workspace: Workspace,
    viewer: Actor,
    client: TestClient,
) -> None:
    """权限范围内什么都没有的用户，会看到一段明确的空状态。"""
    _seed(session, workspace, visibility="TEAM")

    assert web_login(client, viewer).status_code == 303
    page = client.get(BASE)

    assert page.status_code == 200
    assert "当前权限范围内没有匹配的报告。" in page.text


def test_detail_page_is_refused_outside_the_scope(
    session: Session,
    workspace: Workspace,
    viewer: Actor,
    client: TestClient,
) -> None:
    """`TEAM` 报告对「个人」范围的只读用户不可见。"""
    case = _seed(session, workspace, visibility="TEAM")
    assert web_login(client, viewer).status_code == 303

    response = client.get(f"{BASE}/{case.report_id}")

    assert response.status_code == 403
    assert "403 无权访问" in response.text
    assert "当前用户无权查看该报告" in response.text


def test_unknown_report_renders_the_not_found_page(client: TestClient, admin: Actor) -> None:
    """报告不存在时渲染 404 页面，而不是一个 API 错误响应体。"""
    assert web_login(client, admin).status_code == 303

    response = client.get(f"{BASE}/987654")

    assert response.status_code == 404
    assert "404 未找到" in response.text


def test_read_only_role_sees_no_actions(
    session: Session,
    workspace: Workspace,
    viewer: Actor,
    client: TestClient,
) -> None:
    """只读用户读得到 `INTERNAL` 报告，但不会被提供任何写控件。"""
    case = _seed(session, workspace, visibility="INTERNAL")
    assert web_login(client, viewer).status_code == 303

    page = client.get(f"{BASE}/{case.report_id}")

    assert page.status_code == 200
    assert "当前角色只读，不能验证、审核、发布、编辑、回退或推送报告。" in page.text
    assert f'action="{BASE}/{case.report_id}/verify"' not in page.text
    assert f'action="{BASE}/{case.report_id}/push"' not in page.text


def test_read_only_role_cannot_mutate(
    session: Session,
    workspace: Workspace,
    viewer: Actor,
    client: TestClient,
) -> None:
    """写角色守卫在服务层之前就拒掉了只读用户。"""
    case = _seed(session, workspace, visibility="INTERNAL")
    assert web_login(client, viewer).status_code == 303

    response = client.post(f"{BASE}/{case.report_id}/verify", data=_token(client))

    assert response.status_code == 403
    assert "只读角色不能执行该操作" in response.text


def _denials_by_action(factory: sessionmaker[Session]) -> dict[str, set[str | None]]:
    """返回每一种被拒动作各自记在了哪个资源 id 上。"""
    with factory() as fresh:
        rows = fresh.scalars(select(AuditLog).where(AuditLog.result == "denied")).all()
        result: dict[str, set[str | None]] = {}
        for row in rows:
            result.setdefault(row.action, set()).add(row.resource_id)
        return result


def test_refused_ui_actions_are_audited(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    viewer: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """界面上的每一次拒绝都留下一行审计，点名动作、报告和操作者。

    界面才是运维者真正在用的那一面，所以一次只渲染 403 页面、什么都不记的拒绝，
    会让审计轨迹里只剩 JSON API 那一半故事。动作名刻意与 API 共用：不能写的角色
    记 `auth.rbac`，令牌对不上记 `auth.csrf`，报告越权记 `report.read`。
    """
    internal = _seed(session, workspace, visibility="INTERNAL")
    out_of_scope = _seed(session, workspace, visibility="PRIVATE")

    assert web_login(client, viewer).status_code == 303
    read_only = client.post(f"{BASE}/{internal.report_id}/verify", data=_token(client))
    bad_csrf = client.post(
        f"{BASE}/{internal.report_id}/verify",
        data={"csrf_token": "not-the-cookie"},
    )

    web_logout(client)
    assert web_login(client, editor).status_code == 303
    unreadable = client.post(f"{BASE}/{out_of_scope.report_id}/verify", data=_token(client))

    assert (read_only.status_code, bad_csrf.status_code, unreadable.status_code) == (403, 403, 403)

    denials = _denials_by_action(session_factory)
    assert denials["auth.rbac"] == {str(internal.report_id)}
    assert denials["auth.csrf"] == {str(internal.report_id)}
    assert denials["report.read"] == {str(out_of_scope.report_id)}


# --------------------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------------------


def test_detail_page_renders_evidence_and_history(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """一页里同时装着结论、引用、版本与审核。"""
    case = _seed(session, workspace)
    assert web_login(client, editor).status_code == 303
    client.post(f"{BASE}/{case.report_id}/verify", data=_token(client))

    page = client.get(f"{BASE}/{case.report_id}")

    assert page.status_code == 200
    assert load_report(session_factory, case.report_id).title in page.text
    assert "第 1 版 /" in page.text
    assert "passed" in page.text
    assert "引用与证据（第 1 版）" in page.text
    assert "引用覆盖率" in page.text
    assert "版本历史" in page.text
    assert "审核记录" in page.text
    assert "还没有审核记录。" in page.text
    assert "报告正文" in page.text


def test_list_supports_status_filtering(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """筛选表单会收窄列表，并让已选项保持选中。"""
    published = _seed(session, workspace)
    _seed(session, workspace)
    configure_report(session, report_id=published.report_id, status="published")
    assert web_login(client, editor).status_code == 303

    filtered = client.get(f"{BASE}?status=published")

    assert filtered.status_code == 200
    assert f"/ui/reports/{published.report_id}" in filtered.text
    assert '<option value="published" selected>' in filtered.text


# --------------------------------------------------------------------------------------
# 校验、评审与发布
# --------------------------------------------------------------------------------------


def test_verify_action_swaps_the_panel_and_persists_the_verdict(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """HTMX 动作会重渲染面板，而结论在重新读取之后依然在。"""
    case = _seed(session, workspace)
    assert web_login(client, editor).status_code == 303

    response = client.post(f"{BASE}/{case.report_id}/verify", data=_token(client))

    assert response.status_code == 200
    assert "验证结论：passed" in response.text

    # 用第二个会话把那一行读回来，这样断言就不会被「面板渲染了它刚在内存里改过的
    # 对象」所满足。
    verification = _detail(session_factory, case.report_id).verification
    assert verification is not None
    assert verification.verdict == "passed"
    assert verification.structure_ok is True
    assert verification.citations_ok is True
    assert verification.numbers_ok is True
    assert verification.sensitivity_ok is True


def test_manual_review_and_release_through_the_ui(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """`MANUAL` 先排队，评审表单批准之后，下一次尝试才放行。"""
    case = _seed(
        session,
        workspace,
        publish_mode=MANUAL,
        visibility="INTERNAL",
        prior_publication=True,
    )
    assert web_login(client, editor).status_code == 303
    token = _token(client)

    queued = client.post(f"{BASE}/{case.report_id}/publish", data=token)
    assert queued.status_code == 200
    assert "已转入人工审核" in queued.text
    assert load_report(session_factory, case.report_id).status == "pending_review"

    reviewed = client.post(
        f"{BASE}/{case.report_id}/review",
        data={**token, "decision": "approve", "comment": "来源与数字已核对"},
    )
    assert reviewed.status_code == 200
    assert "审核已记录：通过" in reviewed.text
    assert "来源与数字已核对" in reviewed.text
    assert load_report(session_factory, case.report_id).status == "approved"

    released = client.post(f"{BASE}/{case.report_id}/publish", data=token)
    assert released.status_code == 200
    assert "报告已发布" in released.text
    assert load_report(session_factory, case.report_id).status == "published"


def test_review_can_send_a_report_back_for_changes(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """`request_changes` 是 T12「退回报告」那条需求在界面上的那一半。"""
    case = _seed(session, workspace, publish_mode=MANUAL, visibility="INTERNAL")
    assert web_login(client, editor).status_code == 303
    token = _token(client)
    client.post(f"{BASE}/{case.report_id}/publish", data=token)

    returned = client.post(
        f"{BASE}/{case.report_id}/review",
        data={**token, "decision": "request_changes", "comment": "补充趋势对比"},
    )

    assert returned.status_code == 200
    assert "审核已记录：退回修改" in returned.text
    assert load_report(session_factory, case.report_id).status == "changes_requested"


def test_publish_due_action_reports_the_sweep(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """手动扫描复用的，正是定时 Celery 任务那套驱动。"""
    case = _seed(
        session,
        workspace,
        publish_mode=AUTO_WITH_DELAY,
        visibility="INTERNAL",
        prior_publication=True,
    )
    assert web_login(client, editor).status_code == 303
    token = _token(client)

    queued = client.post(f"{BASE}/{case.report_id}/publish", data=token)
    assert "已进入发布队列" in queued.text
    assert load_report(session_factory, case.report_id).status == "scheduled"

    # 把撤回窗口拨到过去，而不是真的等它走完。
    configure_report(session, report_id=case.report_id, publish_at=NOW - timedelta(minutes=1))
    sweep = client.post(f"{BASE}/publish-due", data=token)

    assert sweep.status_code == 200
    assert "释放 1 条，被拦截 0 条" in sweep.text
    assert load_report(session_factory, case.report_id).status == "published"


# --------------------------------------------------------------------------------------
# 编辑与回滚
# --------------------------------------------------------------------------------------


def test_edit_action_appends_a_version(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """保存表单会生成新版本，并把报告打回待复核状态。"""
    case = _seed(session, workspace)
    assert web_login(client, editor).status_code == 303
    original = load_report(session_factory, case.report_id).content_markdown

    response = client.post(
        f"{BASE}/{case.report_id}/edit",
        data={
            **_token(client),
            "title": "",
            "content_markdown": original + "\n人工补充说明。\n",
            "visibility": "",
            "sensitivity": "",
        },
    )

    assert response.status_code == 200
    assert "内容已更新为第 2 版" in response.text
    report = load_report(session_factory, case.report_id)
    assert report.status == "draft"
    assert report.current_version == 2
    assert report.content_markdown.endswith("人工补充说明。\n")


def test_edit_form_is_withheld_outside_the_editable_statuses(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """已发布的报告只提供回滚，不提供重写。"""
    case = _seed(session, workspace)
    configure_report(session, report_id=case.report_id, status="published")
    assert web_login(client, editor).status_code == 303

    page = client.get(f"{BASE}/{case.report_id}")

    assert page.status_code == 200
    assert "不能直接编辑正文" in page.text
    assert f'action="{BASE}/{case.report_id}/edit"' not in page.text


def test_rollback_action_restores_a_historical_version(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """从版本表里回滚是追加一个版本，而不是把历史倒带。"""
    case = _seed(session, workspace)
    assert web_login(client, editor).status_code == 303
    original = load_report(session_factory, case.report_id).content_markdown

    edited = client.post(
        f"{BASE}/{case.report_id}/edit",
        data={**_token(client), "content_markdown": original + "\n一次错误的修改。\n"},
    )
    assert "内容已更新为第 2 版" in edited.text

    rolled = client.post(
        f"{BASE}/{case.report_id}/rollback",
        data={**_token(client), "version": 1, "comment": "回退到上周口径"},
    )

    assert rolled.status_code == 200
    assert "已回滚到第 1 版，新版本为第 3 版" in rolled.text
    report = load_report(session_factory, case.report_id)
    assert report.content_markdown == original
    assert report.current_version == 3
    assert report.status == "draft"
    assert ("report.rollback", "success") in audit_trail(session_factory)


@pytest.mark.parametrize("status", ["pending_review", "approved", "scheduled"])
def test_rollback_is_not_offered_on_the_publishing_path(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
    status: str,
) -> None:
    """按钮与服务端守卫一致，而硬发那个请求也弄不坏页面。

    模板曾经只在 `failed` 时隐藏按钮，于是这三种状态都会渲染出一个服务层会拒绝的
    按钮；而那次拒绝是在报告对象已经被改写 *之后* 才到达面板渲染器的，页面于是
    显示出错误的版本与正文。
    """
    case = _seed(session, workspace)
    assert web_login(client, editor).status_code == 303
    original = load_report(session_factory, case.report_id).content_markdown

    edited = client.post(
        f"{BASE}/{case.report_id}/edit",
        data={**_token(client), "content_markdown": original + "\n第二版内容。\n"},
    )
    assert "内容已更新为第 2 版" in edited.text
    configure_report(session, report_id=case.report_id, status=status)

    page = client.get(f"{BASE}/{case.report_id}")
    assert page.status_code == 200
    assert f'action="{BASE}/{case.report_id}/rollback"' not in page.text

    forced = client.post(
        f"{BASE}/{case.report_id}/rollback",
        data={**_token(client), "version": 1},
    )

    # 是一次渲染出来的拒绝，而不是崩溃；面板显示的仍是没被篡改的那份报告。
    assert forced.status_code == 200
    assert "第 2 版" in forced.text
    report = load_report(session_factory, case.report_id)
    assert report.status == status
    assert report.current_version == 2
    assert report.content_markdown.endswith("第二版内容。\n")


def test_published_report_offers_withdraw_and_returns_to_draft(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """已发布的报告只有一条回头路，页面必须把它给出来。

    没有这个按钮，报告就卡死了：`edit` 要求草稿，`rollback` 要求有第二个版本，
    别的动作都不接受 `published`。页面曾经印一句提示，让读者「退回修改或回滚」——
    而这两个操作从这里都走不到。
    """
    case = _seed(session, workspace)
    configure_report(session, report_id=case.report_id, status="published")
    assert web_login(client, editor).status_code == 303

    page = client.get(f"{BASE}/{case.report_id}")
    assert page.status_code == 200
    assert f'action="{BASE}/{case.report_id}/withdraw"' in page.text
    assert "撤销发布" in page.text

    withdrawn = client.post(f"{BASE}/{case.report_id}/withdraw", data={**_token(client)})

    assert withdrawn.status_code == 200
    assert "已撤销发布" in withdrawn.text
    report = load_report(session_factory, case.report_id)
    assert report.status == "draft"
    assert report.published_at is None
    # 撤销之后页面本身也换了：编辑表单回来了，撤销按钮走了。
    assert f'action="{BASE}/{case.report_id}/edit"' in withdrawn.text
    assert f'action="{BASE}/{case.report_id}/withdraw"' not in withdrawn.text
    # 一次成功撤销就是一行审计，动作名与 API 侧一致。
    assert ("report.withdraw", "success") in audit_trail(session_factory)


def test_withdraw_is_refused_on_a_report_that_is_not_published(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """对草稿硬发一次撤销，会渲染出拒绝，并且报告原地不动。"""
    case = _seed(session, workspace)
    assert web_login(client, editor).status_code == 303

    response = client.post(f"{BASE}/{case.report_id}/withdraw", data={**_token(client)})

    assert response.status_code == 200
    assert "only a published report can be withdrawn" in response.text
    assert load_report(session_factory, case.report_id).status == "draft"


# --------------------------------------------------------------------------------------
# 首页
# --------------------------------------------------------------------------------------


def test_home_renders_system_state_and_newest_report(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """落地页回答三件事：它在做什么、最近一期报告、怎么触发。"""
    case = _seed(session, workspace)
    assert web_login(client, editor).status_code == 303

    response = client.get("/ui/home")

    assert response.status_code == 200
    assert "系统状态" in response.text
    assert "最近一期报告" in response.text
    assert f"/ui/reports/{case.report_id}" in response.text
    # 编辑持有写角色，所以触发卡片会给出来。
    assert "一键触发" in response.text
    assert "立即采集" in response.text
    assert "立即研究" in response.text


def test_home_hides_triggers_from_a_viewer(
    session: Session,
    workspace: Workspace,
    viewer: Actor,
    client: TestClient,
) -> None:
    """只读用户看得到状态与最近一期报告，但没有任何触发按钮。"""
    _seed(session, workspace)
    assert web_login(client, viewer).status_code == 303

    response = client.get("/ui/home")

    assert response.status_code == 200
    assert "系统状态" in response.text
    assert "最近一期报告" in response.text
    assert "一键触发" not in response.text
    assert "立即采集" not in response.text


def test_home_trigger_answers_htmx_with_a_fragment(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """从页面上触发的动作只能回一小段提示，不能回第二个页面。

    触发表单的目标是 `#home-trigger-result`，并用 `hx-swap="innerHTML"`，
    所以端点返回什么，就会被 *塞进* 那张卡片。返回整份模板曾把外壳 —— 连侧边栏
    在内 —— 一起塞进卡片，读者看到的是平台嵌套着自己。
    """
    _seed(session, workspace)
    assert web_login(client, editor).status_code == 303
    monkeypatch.setattr(web_router, "_queue_collect", lambda settings: "abc12345def")

    response = client.post(
        "/ui/home/collect",
        data={**_token(client)},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "已排队采集任务" in response.text
    assert "abc12345" in response.text
    # 片段不含文档外壳，也不含第二份侧栏与那个被替换的容器本身。
    assert "<!DOCTYPE" not in response.text
    assert 'class="sidebar"' not in response.text
    assert 'id="home-trigger-result"' not in response.text


def test_home_trigger_without_htmx_still_renders_the_page(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不带 JavaScript 的普通表单提交，仍然拿回整页，提示也照旧在里面。"""
    _seed(session, workspace)
    assert web_login(client, editor).status_code == 303
    monkeypatch.setattr(web_router, "_queue_collect", lambda settings: "abc12345def")

    response = client.post("/ui/home/collect", data={**_token(client)})

    assert response.status_code == 200
    assert "<!DOCTYPE" in response.text
    assert "已排队采集任务" in response.text
    assert "系统状态" in response.text


def test_home_without_topics_disables_the_research_tile(
    client: TestClient,
    editor: Actor,
) -> None:
    """没有已启用的主题时，研究卡片指向怎么修，而不是给一个按不动的按钮。"""
    assert web_login(client, editor).status_code == 303

    response = client.get("/ui/home")

    assert response.status_code == 200
    assert "一键触发" in response.text
    assert "无已启用主题" in response.text
    assert "disabled" in response.text


def test_login_without_next_lands_on_home(client: TestClient, editor: Actor) -> None:
    """登录后没给 `next` 时，默认落到落地页，而不是报告列表。"""
    token = prime_csrf(client)
    response = client.post(
        "/ui/login",
        data={
            "csrf_token": token,
            "username": editor.username,
            "password": "correct horse battery staple",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/home"
