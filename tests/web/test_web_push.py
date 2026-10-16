"""Integration tests for the push card in the server-rendered admin UI.

They drive the T13 UI path over the same real route/service/persistence stack the JSON API
tests use, and check the two things the UI adds on top: the action is gated by the cookie
session plus the double-submit CSRF token, and the channel is taken from the allowlist row
rather than from the browser.
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
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """An administrator, who may both read every report and push."""
    return create_actor(session, username="ui-push-admin", role="ADMIN")


@pytest.fixture()
def editor(session: Session) -> Actor:
    """An editor on the team scope, holding the other write role."""
    return create_actor(session, username="ui-push-editor", role="EDITOR", data_scope="team")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """A read-only user, who must not be able to push at all."""
    return create_actor(session, username="ui-push-viewer", role="VIEWER")


def _token(client: TestClient) -> dict[str, str]:
    """Return the CSRF form payload for a mutating request."""
    return {"csrf_token": csrf_value(client)}


def _push_audit(session_factory: sessionmaker[Session]) -> list[tuple[str, str]]:
    """Return only the push entries, so seeding noise does not leak into an assertion."""
    return [entry for entry in audit_trail(session_factory) if entry[0] == "report.push"]


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def test_the_panel_offers_the_allowlist_and_the_delivery_history(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """The card lists the delivery hint and each allowlisted destination."""
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
    """Without an allowlist entry the card explains why, in terms its reader can act on.

    Adding a push target is deployment configuration written from the backend, so an editor cannot
    do it. Telling an editor to go write `push_targets` describes an action they do not have — and
    hands them a table name they have no business matching against. The admin, who can act on it,
    gets the table; the editor gets the admin.
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
# Delivering
# --------------------------------------------------------------------------------------


def test_pushing_from_the_panel_records_the_delivery(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A form post delivers the report and the refreshed panel shows the sent row."""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    target = workspace.add_push_target()
    assert web_login(client, editor).status_code == 303

    response = client.post(
        f"{BASE}/{report.id}/push",
        data={**_token(client), "target_id": target.id},
    )

    assert response.status_code == 200, response.text
    assert f"已推送到 {target.destination}（第 1 次尝试）" in response.text
    assert 'class="ok-text">sent</td>' in response.text
    assert ("report.push", "success") in audit_trail(session_factory)


def test_a_second_post_from_the_panel_replays_instead_of_resending(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """The same target twice is a replay, and the panel says so."""
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
    """A draft never leaves the building, and the panel comes back with the reason."""
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
    """A forged channel field is ignored; the target row decides which transport is used."""
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
    """A target id that is not on the allowlist is refused with a readable code."""
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
# Guards
# --------------------------------------------------------------------------------------


def test_pushing_through_the_ui_needs_a_csrf_token(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """An omitted token is stopped before the service is reached."""
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
    """The write-role guard answers before the push service is reached."""
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
    """No session means a redirect back to the login page, token or not."""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    token = prime_csrf(client)

    response = client.post(
        f"{BASE}/{report.id}/push",
        data={"csrf_token": token, "target_id": 1},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/login")
