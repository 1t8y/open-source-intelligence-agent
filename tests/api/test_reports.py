"""守护报告 API：列表、读取、编辑、校验、评审、发布、回滚这条验收链路。

用例用 HTTP 把 T12 的验收路径整条走一遍，并覆盖守着它的访问控制。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from api_support import (
    Actor,
    audit_trail,
    auth_headers,
    build_client,
    configure_report,
    create_actor,
    load_report,
    source_versions,
)
from fastapi.testclient import TestClient
from reports_support import (
    AUTO,
    AUTO_WITH_DELAY,
    MANUAL,
    SeededReport,
    build_report,
    seed_published_sibling,
)
from sqlalchemy.orm import Session, sessionmaker
from support import NOW, Workspace

BASE = "/api/v1/reports"


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


# --------------------------------------------------------------------------------------
# 访问控制
# --------------------------------------------------------------------------------------


def test_list_is_filtered_by_visibility_inside_the_query(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    editor: Actor,
    viewer: Actor,
    client: TestClient,
) -> None:
    """每个角色看到的，恰好是它可见范围允许的那些报告。"""
    internal = _seed(session, workspace, visibility="INTERNAL")
    team = _seed(session, workspace, visibility="TEAM")
    private = _seed(session, workspace, visibility="PRIVATE")
    configure_report(session, report_id=private.report_id, owner_id=editor.user_id)

    viewer_body = client.get(BASE, headers=auth_headers(client, viewer)).json()
    assert [item["id"] for item in viewer_body["items"]] == [internal.report_id]
    assert viewer_body["total"] == 1

    editor_body = client.get(BASE, headers=auth_headers(client, editor)).json()
    assert {item["id"] for item in editor_body["items"]} == {
        internal.report_id,
        team.report_id,
        private.report_id,
    }
    assert editor_body["total"] == 3

    admin_body = client.get(BASE, headers=auth_headers(client, admin)).json()
    assert admin_body["total"] == 3
    assert all(item["topic_name"] for item in admin_body["items"])


def test_private_report_without_an_owner_is_admin_only(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    admin: Actor,
    client: TestClient,
) -> None:
    """没记下归属人的 `PRIVATE` 报告对非管理员始终不可见。"""
    case = _seed(session, workspace, visibility="PRIVATE")

    assert auth_headers(client, editor)
    editor_response = client.get(
        f"{BASE}/{case.report_id}",
        headers=auth_headers(client, editor),
    )
    admin_response = client.get(
        f"{BASE}/{case.report_id}",
        headers=auth_headers(client, admin),
    )

    assert editor_response.status_code == 403
    assert admin_response.status_code == 200


def test_reading_outside_the_scope_returns_403_without_metadata(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    viewer: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """被拒的读取不能泄露报告内容，而且必须进审计。"""
    case = _seed(session, workspace, visibility="PRIVATE")
    configure_report(session, report_id=case.report_id, owner_id=editor.user_id)

    response = client.get(
        f"{BASE}/{case.report_id}",
        headers=auth_headers(client, viewer),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert case.reload(session).title not in response.text
    assert ("report.read", "denied") in audit_trail(session_factory)


def test_unknown_report_is_a_problem_details_not_found(
    admin: Actor,
    client: TestClient,
) -> None:
    """报告不存在时返回 404，并带上 RFC 7807 的各成员。"""
    response = client.get(f"{BASE}/987654", headers=auth_headers(client, admin))

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "NOT_FOUND"
    assert body["status"] == 404
    assert body["type"].endswith("/not-found")
    assert body["instance"] == f"{BASE}/987654"
    assert body["request_id"]


def test_anonymous_requests_are_rejected(client: TestClient) -> None:
    """每一条报告路由都要 Bearer 令牌。"""
    assert client.get(BASE).status_code == 401
    assert client.post(f"{BASE}/1/publish").status_code == 401


def test_viewer_cannot_write(
    session: Session,
    workspace: Workspace,
    viewer: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """只读角色在进入服务层之前就被拒了。"""
    case = _seed(session, workspace)

    response = client.patch(
        f"{BASE}/{case.report_id}",
        json={"title": "hijacked"},
        headers=auth_headers(client, viewer),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert load_report(session_factory, case.report_id).title != "hijacked"
    assert ("auth.rbac", "denied") in audit_trail(session_factory)


# --------------------------------------------------------------------------------------
# 编辑
# --------------------------------------------------------------------------------------


def test_edit_appends_an_immutable_version_and_returns_to_draft(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """改正文会生成新版本，并把报告打回待复核状态。"""
    case = _seed(session, workspace)
    original = load_report(session_factory, case.report_id).content_markdown
    edited = original + "\n人工补充说明。\n"

    response = client.patch(
        f"{BASE}/{case.report_id}",
        json={"content_markdown": edited},
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 2
    assert body["content_changed"] is True
    assert body["changed_fields"] == ["content_markdown"]
    report = load_report(session_factory, case.report_id)
    assert report.status == "draft"
    assert report.current_version == 2
    assert report.content_markdown == edited
    # 新版本继承上一版的引用集合，这样它仍然具备发布条件。
    assert source_versions(session_factory, case.report_id) == {1, 2}

    detail = client.get(
        f"{BASE}/{case.report_id}",
        headers=auth_headers(client, editor),
    ).json()
    assert [row["version"] for row in detail["versions"]] == [2, 1]
    assert detail["versions"][0]["change_reason"] == "manual_edit"
    assert ("report.edit", "success") in audit_trail(session_factory)


def test_metadata_only_edit_keeps_the_version(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """改 `visibility` 不会产生新版本，因为交付物本身没变。"""
    case = _seed(session, workspace)

    response = client.patch(
        f"{BASE}/{case.report_id}",
        json={"visibility": "INTERNAL", "sensitivity": "PUBLIC"},
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert response.json()["content_changed"] is False
    report = load_report(session_factory, case.report_id)
    assert report.visibility == "INTERNAL"
    assert report.sensitivity == "PUBLIC"


def test_edit_is_refused_outside_the_editable_statuses(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """已发布的报告必须走回滚，而不是原地改写。"""
    case = _seed(session, workspace)
    configure_report(session, report_id=case.report_id, status="published")

    response = client.patch(
        f"{BASE}/{case.report_id}",
        json={"title": "rewritten"},
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "REPORT_STATE_INVALID"


def test_edit_rejects_an_unknown_visibility(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """请求体在到达服务层之前就完成了校验。"""
    case = _seed(session, workspace)

    response = client.patch(
        f"{BASE}/{case.report_id}",
        json={"visibility": "SECRET"},
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------------------
# 校验、评审与发布
# --------------------------------------------------------------------------------------


def test_verify_returns_and_stores_the_verdict(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """经由 HTTP 跑一次校验器，会为当前版本落下一条通过的结论。"""
    case = _seed(session, workspace)

    response = client.post(
        f"{BASE}/{case.report_id}/verify",
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "passed"
    assert body["structure_ok"] is True
    assert body["citations_ok"] is True
    assert body["numbers_ok"] is True
    assert body["sensitivity_ok"] is True
    assert body["citation_coverage"] == 1.0

    detail = client.get(
        f"{BASE}/{case.report_id}",
        headers=auth_headers(client, editor),
    ).json()
    assert detail["verification"]["verdict"] == "passed"
    assert len(detail["sources"]) > 0


def test_auto_report_is_released_by_the_publish_action(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """`AUTO` 没有撤回窗口，所以发布动作会立刻放行。"""
    case = _seed(
        session, workspace, publish_mode=AUTO, visibility="INTERNAL", prior_publication=True
    )

    response = client.post(
        f"{BASE}/{case.report_id}/publish",
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "published"
    assert body["publish_decision"]["resolution"] == "scheduled"
    assert body["publish_decision"]["reason_codes"] == []
    assert body["publish_decision"]["requires_review"] is False
    assert body["verification"]["verdict"] == "passed"
    assert load_report(session_factory, case.report_id).status == "published"


def test_delayed_report_waits_for_its_window(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """`AUTO_WITH_DELAY` 进入队列，在到期之前扫描不会把它放出去。"""
    case = _seed(session, workspace, publish_mode=AUTO_WITH_DELAY, prior_publication=True)

    response = client.post(
        f"{BASE}/{case.report_id}/publish",
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "scheduled"
    assert body["publish_decision"]["resolution"] == "scheduled"
    assert body["publish_decision"]["publish_at"] is not None

    sweep = client.post(
        f"{BASE}/publish-due",
        headers=auth_headers(client, editor),
    )
    assert sweep.status_code == 200
    assert sweep.json()["released_count"] == 0
    assert load_report(session_factory, case.report_id).status == "scheduled"


def test_manual_report_goes_to_review_then_releases_after_approval(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """`MANUAL` 这条路要先留下批准记录，同一个动作才会真正放行。"""
    case = _seed(
        session, workspace, publish_mode=MANUAL, visibility="INTERNAL", prior_publication=True
    )
    headers = auth_headers(client, editor)

    first = client.post(f"{BASE}/{case.report_id}/publish", headers=headers).json()
    assert first["status"] == "pending_review"
    assert first["publish_decision"]["requires_review"] is True
    assert first["publish_decision"]["publish_at"] is None
    assert load_report(session_factory, case.report_id).status == "pending_review"

    review = client.post(
        f"{BASE}/{case.report_id}/review",
        json={"decision": "approve", "comment": "来源与数字已核对"},
        headers=headers,
    )
    assert review.status_code == 200
    assert review.json()["status"] == "approved"
    assert review.json()["decision"] == "approve"

    second = client.post(f"{BASE}/{case.report_id}/publish", headers=headers)
    assert second.status_code == 200
    assert second.json()["status"] == "published"
    assert load_report(session_factory, case.report_id).status == "published"


def test_manual_release_is_blocked_without_an_approval(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """`INV-5` 的额外闸门：排队中的 `MANUAL` 报告没有批准就不能放行。"""
    case = _seed(session, workspace, publish_mode=MANUAL, visibility="INTERNAL")
    configure_report(
        session,
        report_id=case.report_id,
        status="scheduled",
        publish_at=NOW - timedelta(minutes=1),
    )

    response = client.post(
        f"{BASE}/{case.report_id}/publish",
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "PUBLISH_POLICY_BLOCKED"
    assert load_report(session_factory, case.report_id).status == "scheduled"


def test_review_decisions_map_to_the_state_machine(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """`request_changes` 把报告打回，`reject` 则让它走向终止。"""
    case = _seed(session, workspace, publish_mode=MANUAL, visibility="INTERNAL")
    headers = auth_headers(client, editor)
    client.post(f"{BASE}/{case.report_id}/publish", headers=headers)

    returned = client.post(
        f"{BASE}/{case.report_id}/review",
        json={"decision": "request_changes", "comment": "补充趋势对比"},
        headers=headers,
    )
    assert returned.status_code == 200
    assert returned.json()["status"] == "changes_requested"

    # 被打回的报告还能再改，改了之后就回到草稿队列。
    edited = client.patch(
        f"{BASE}/{case.report_id}",
        json={
            "content_markdown": load_report(session_factory, case.report_id).content_markdown + "\n"
        },
        headers=headers,
    )
    assert edited.status_code == 200
    assert load_report(session_factory, case.report_id).status == "draft"

    client.post(f"{BASE}/{case.report_id}/publish", headers=headers)
    rejected = client.post(
        f"{BASE}/{case.report_id}/review",
        json={"decision": "reject", "comment": "口径不符"},
        headers=headers,
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "failed"
    assert load_report(session_factory, case.report_id).status == "failed"


def test_sweep_reports_blocked_entries(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """一次受阻的放行会被报出来，而且不会中断整次扫描。"""
    case = _seed(session, workspace, publish_mode=MANUAL, visibility="INTERNAL")
    configure_report(
        session,
        report_id=case.report_id,
        status="scheduled",
        publish_at=NOW - timedelta(minutes=1),
    )

    body = client.post(f"{BASE}/publish-due", headers=auth_headers(client, editor)).json()

    assert body["released_count"] == 0
    assert body["blocked_count"] == 1
    assert body["released"][0]["error_code"] == "PUBLISH_POLICY_BLOCKED"


# --------------------------------------------------------------------------------------
# 回滚
# --------------------------------------------------------------------------------------


def test_rollback_restores_a_historical_version_as_a_new_draft(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """回滚是追加一个版本，而不是把历史倒带。"""
    case = _seed(session, workspace)
    headers = auth_headers(client, editor)
    original = load_report(session_factory, case.report_id).content_markdown
    client.patch(
        f"{BASE}/{case.report_id}",
        json={"content_markdown": original + "\n一次错误的修改。\n"},
        headers=headers,
    )

    response = client.post(
        f"{BASE}/{case.report_id}/rollback",
        json={"version": 1, "comment": "回退到上周口径"},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["restored_from"] == 1
    assert body["version"] == 3
    assert body["status"] == "draft"
    report = load_report(session_factory, case.report_id)
    assert report.content_markdown == original
    assert report.current_version == 3
    assert source_versions(session_factory, case.report_id) == {1, 2, 3}
    assert ("report.rollback", "success") in audit_trail(session_factory)

    detail = client.get(f"{BASE}/{case.report_id}", headers=headers).json()
    reasons = {row["change_reason"] for row in detail["versions"]}
    assert "rollback_to_v1" in reasons


def test_rollback_rejects_the_current_version(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """回滚到当前生效的那个版本，属于坏请求。"""
    case = _seed(session, workspace)

    response = client.post(
        f"{BASE}/{case.report_id}/rollback",
        json={"version": 1},
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_rollback_rejects_an_unknown_version(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """从没记录过的版本是 404，而不是悄悄什么也不做。"""
    case = _seed(session, workspace)

    response = client.post(
        f"{BASE}/{case.report_id}/rollback",
        json={"version": 99},
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_rollback_is_refused_for_a_failed_report(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """被驳回的报告停在终态，回滚救不回来。"""
    case = _seed(session, workspace)
    configure_report(session, report_id=case.report_id, status="failed")

    response = client.post(
        f"{BASE}/{case.report_id}/rollback",
        json={"version": 1},
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "REPORT_STATE_INVALID"


# --------------------------------------------------------------------------------------
# 列表
# --------------------------------------------------------------------------------------


def test_list_supports_filters_and_pagination(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """状态筛选与页长都生效，总数也对得上。"""
    first = _seed(session, workspace)
    _seed(session, workspace)
    configure_report(session, report_id=first.report_id, status="published")
    headers = auth_headers(client, editor)

    published = client.get(f"{BASE}?status=published", headers=headers).json()
    assert published["total"] == 1
    assert published["items"][0]["id"] == first.report_id

    page = client.get(f"{BASE}?page_size=1&page=2", headers=headers).json()
    assert page["page"] == 2
    assert page["page_size"] == 1
    assert page["total"] == 2
    assert len(page["items"]) == 1


def test_list_rejects_an_out_of_range_page_size(
    editor: Actor,
    client: TestClient,
) -> None:
    """`page_size` 遵守文档写明的 1–100 边界。"""
    response = client.get(
        f"{BASE}?page_size=101",
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_list_refuses_a_timestamp_without_an_offset(
    editor: Actor,
    client: TestClient,
) -> None:
    """`created_after` 必须说明自己处在哪个时区。

    `repositories.first_seen_at` 与 `reports.generated_at` 都是 `UTCDateTime`，
    它拒绝为朴素值猜测时区。T18 之前，这种拒绝会从查询构造器内部以 `500` 的形式
    抛给调用方 —— 这个参数从来没被实际用过，所以没人发现。共享的 `UtcDateTime`
    参数类型把它变成契约承诺的 `422`。
    """
    response = client.get(
        f"{BASE}?created_after=2026-09-24T00:00:00",
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert "timezone offset" in response.json()["detail"]


def test_list_accepts_an_offset_timestamp(
    editor: Actor,
    client: TestClient,
) -> None:
    """同一个参数，只要指明了时区就能用。"""
    response = client.get(
        f"{BASE}?created_after=2026-09-24T00:00:00Z",
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 200
