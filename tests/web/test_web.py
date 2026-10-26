"""Integration tests for the server-rendered admin UI.

They drive the same acceptance path as the JSON API tests — list, read, edit, verify,
review, publish, and roll back — through the Jinja2/HTMX pages, and check the two guards the
UI adds on top: the cookie session and the double-submit CSRF token.
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
from open_source_intelligence.web.security import SESSION_COOKIE

BASE = "/ui/reports"


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """An administrator with a personal data scope."""
    return create_actor(session, username="admin", role="ADMIN")


@pytest.fixture()
def editor(session: Session) -> Actor:
    """An editor on the team data scope, able to read and write team reports."""
    return create_actor(session, username="editor", role="EDITOR", data_scope="team")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """A read-only user with a personal data scope."""
    return create_actor(session, username="viewer", role="VIEWER")


def _seed(
    session: Session,
    workspace: Workspace,
    *,
    publish_mode: str = AUTO_WITH_DELAY,
    visibility: str = "TEAM",
    prior_publication: bool = False,
) -> SeededReport:
    """Build a report and optionally give its topic an earlier published report."""
    case = build_report(session, workspace, publish_mode=publish_mode)
    configure_report(session, report_id=case.report_id, visibility=visibility)
    if prior_publication:
        seed_published_sibling(session, case)
    return case


def _token(client: TestClient) -> dict[str, str]:
    """Return the CSRF form payload for a mutating request."""
    return {"csrf_token": csrf_value(client)}


def _detail(session_factory: sessionmaker[Session], report_id: int) -> queries.ReportDetail:
    """Read the aggregated report detail through a short-lived session."""
    with session_factory() as session:
        detail = queries.get_report_detail(session, report_id=report_id)
    assert detail is not None
    return detail


# --------------------------------------------------------------------------------------
# Session and CSRF
# --------------------------------------------------------------------------------------


def test_anonymous_browsing_redirects_to_login(client: TestClient) -> None:
    """Every page requires a session and remembers where the browser was heading."""
    listing = client.get(BASE, follow_redirects=False)
    assert listing.status_code == 303
    assert listing.headers["location"] == "/ui/login?next=%2Fui%2Freports"

    detail = client.get(f"{BASE}/5", follow_redirects=False)
    assert detail.headers["location"] == "/ui/login?next=%2Fui%2Freports%2F5"


def test_a_tampered_session_cookie_redirects_to_login(client: TestClient) -> None:
    """An unreadable cookie is treated exactly like a missing one."""
    client.cookies.set(SESSION_COOKIE, "not-a-jwt")
    response = client.get(BASE, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/login")


def test_login_page_serves_a_csrf_token(client: TestClient) -> None:
    """The form carries a hidden token that matches the double-submit cookie."""
    response = client.get("/ui/login")
    assert response.status_code == 200
    token = csrf_value(client)
    assert 'name="csrf_token"' in response.text
    assert token in response.text


def test_login_stores_an_httponly_session_cookie(client: TestClient, editor: Actor) -> None:
    """A successful sign-in redirects and keeps the JWT out of JavaScript's reach."""
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
    """A wrong password re-renders the form and records the attempt."""
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
    """Signing out drops the cookie so the next page redirects again."""
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
    """An omitted token reaches the CSRF gate and renders the 403 page, not a JSON error."""
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
    """A token that does not match the cookie is refused before any service runs."""
    case = _seed(session, workspace)
    assert web_login(client, editor).status_code == 303

    response = client.post(
        f"{BASE}/{case.report_id}/verify",
        data={"csrf_token": "not-the-cookie"},
    )

    assert response.status_code == 403
    assert "CSRF 校验失败" in response.text


def test_static_assets_are_served_locally(client: TestClient) -> None:
    """HTMX is vendored so the interface works without outbound network access."""
    response = client.get("/ui/static/htmx.min.js")
    assert response.status_code == 200
    assert "htmx" in response.text


# --------------------------------------------------------------------------------------
# Access control on the pages
# --------------------------------------------------------------------------------------


def test_report_list_hides_reports_outside_the_scope(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    viewer: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """The list is filtered in the query, so an unreadable title never reaches the page."""
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
    """A user with nothing in scope gets an explicit empty state."""
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
    """A `TEAM` report is invisible to a personal-scope viewer."""
    case = _seed(session, workspace, visibility="TEAM")
    assert web_login(client, viewer).status_code == 303

    response = client.get(f"{BASE}/{case.report_id}")

    assert response.status_code == 403
    assert "403 无权访问" in response.text
    assert "当前用户无权查看该报告" in response.text


def test_unknown_report_renders_the_not_found_page(client: TestClient, admin: Actor) -> None:
    """A missing report renders the 404 page rather than an API error body."""
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
    """A viewer may read an `INTERNAL` report but is offered no mutating control."""
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
    """The write-role guard rejects a viewer before the service layer runs."""
    case = _seed(session, workspace, visibility="INTERNAL")
    assert web_login(client, viewer).status_code == 303

    response = client.post(f"{BASE}/{case.report_id}/verify", data=_token(client))

    assert response.status_code == 403
    assert "只读角色不能执行该操作" in response.text


def _denials_by_action(factory: sessionmaker[Session]) -> dict[str, set[str | None]]:
    """Return the resource id each denied audit action was filed against."""
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
    """Every UI refusal leaves an audit row naming the action, the report, and the actor.

    The UI is the surface an operator actually uses, so a refusal that renders a 403 page and
    records nothing would leave the trail holding only the JSON API's half of the story. The
    action names are shared with the API on purpose: `auth.rbac` for a role that may not write,
    `auth.csrf` for a token that does not match, `report.read` for a report out of scope.
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
# Read
# --------------------------------------------------------------------------------------


def test_detail_page_renders_evidence_and_history(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """One page carries the verdict, the citations, the versions, and the reviews."""
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
    """The filter form narrows the list and keeps the selection sticky."""
    published = _seed(session, workspace)
    _seed(session, workspace)
    configure_report(session, report_id=published.report_id, status="published")
    assert web_login(client, editor).status_code == 303

    filtered = client.get(f"{BASE}?status=published")

    assert filtered.status_code == 200
    assert f"/ui/reports/{published.report_id}" in filtered.text
    assert '<option value="published" selected>' in filtered.text


# --------------------------------------------------------------------------------------
# Verify, review, publish
# --------------------------------------------------------------------------------------


def test_verify_action_swaps_the_panel_and_persists_the_verdict(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """The HTMX action re-renders the panel, and the verdict survives a fresh read."""
    case = _seed(session, workspace)
    assert web_login(client, editor).status_code == 303

    response = client.post(f"{BASE}/{case.report_id}/verify", data=_token(client))

    assert response.status_code == 200
    assert "验证结论：passed" in response.text

    # Read the row back through a second session so the assertion cannot be satisfied by the
    # panel rendering the object it just mutated in memory.
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
    """`MANUAL` queues, the review form approves, and the next attempt releases."""
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
    """`request_changes` is the UI half of the T12 "return the report" requirement."""
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
    """The manual sweep reuses the same driver as the scheduled Celery task."""
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

    # Bring the withdrawal window forward instead of waiting for it to elapse.
    configure_report(session, report_id=case.report_id, publish_at=NOW - timedelta(minutes=1))
    sweep = client.post(f"{BASE}/publish-due", data=token)

    assert sweep.status_code == 200
    assert "释放 1 条，被拦截 0 条" in sweep.text
    assert load_report(session_factory, case.report_id).status == "published"


# --------------------------------------------------------------------------------------
# Edit and rollback
# --------------------------------------------------------------------------------------


def test_edit_action_appends_a_version(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Saving the form produces a new version and parks the report for re-verification."""
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
    """A published report is offered a rollback instead of a rewrite."""
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
    """Rolling back from the version table appends a version rather than rewinding."""
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
    """The button matches the server-side guard, and forcing the post cannot corrupt a page.

    The template used to hide the button only for `failed`, so these three statuses rendered a
    button the service refuses; the refusal then reached the panel renderer *after* the report
    object had already been rewritten, so the page showed the wrong version and body.
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

    # A rendered refusal, not a crash, and the panel still shows the untampered report.
    assert forced.status_code == 200
    assert "第 2 版" in forced.text
    report = load_report(session_factory, case.report_id)
    assert report.status == status
    assert report.current_version == 2
    assert report.content_markdown.endswith("第二版内容。\n")


# --------------------------------------------------------------------------------------
# Home page
# --------------------------------------------------------------------------------------


def test_home_renders_system_state_and_newest_report(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """The landing page answers "what is it doing / newest report / how to trigger"."""
    case = _seed(session, workspace)
    assert web_login(client, editor).status_code == 303

    response = client.get("/ui/home")

    assert response.status_code == 200
    assert "系统状态" in response.text
    assert "最近一期报告" in response.text
    assert f"/ui/reports/{case.report_id}" in response.text
    # The editor has the write roles, so the trigger card is offered.
    assert "一键触发" in response.text
    assert "立即采集" in response.text
    assert "立即研究" in response.text


def test_home_hides_triggers_from_a_viewer(
    session: Session,
    workspace: Workspace,
    viewer: Actor,
    client: TestClient,
) -> None:
    """A read-only viewer sees the state and the newest report, but no trigger buttons."""
    _seed(session, workspace)
    assert web_login(client, viewer).status_code == 303

    response = client.get("/ui/home")

    assert response.status_code == 200
    assert "系统状态" in response.text
    assert "最近一期报告" in response.text
    assert "一键触发" not in response.text
    assert "立即采集" not in response.text


def test_login_without_next_lands_on_home(client: TestClient, editor: Actor) -> None:
    """The default post-login destination is the landing page, not the report list."""
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
