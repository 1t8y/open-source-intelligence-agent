"""Integration tests for the report API.

They cover the T12 acceptance path end to end over HTTP: list, read, edit, verify, review,
publish, and roll back — plus the access control that guards all of it.
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


# --------------------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------------------


def test_list_is_filtered_by_visibility_inside_the_query(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    editor: Actor,
    viewer: Actor,
    client: TestClient,
) -> None:
    """Each role sees exactly the reports its visibility scope allows."""
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
    """A `PRIVATE` report with no recorded owner stays invisible to non-admins."""
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
    """A denied read must not leak the report and must be audited."""
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
    """A missing report is a 404 carrying the RFC 7807 members."""
    response = client.get(f"{BASE}/987654", headers=auth_headers(client, admin))

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "NOT_FOUND"
    assert body["status"] == 404
    assert body["type"].endswith("/not-found")
    assert body["instance"] == f"{BASE}/987654"
    assert body["request_id"]


def test_anonymous_requests_are_rejected(client: TestClient) -> None:
    """Every report route requires a bearer token."""
    assert client.get(BASE).status_code == 401
    assert client.post(f"{BASE}/1/publish").status_code == 401


def test_viewer_cannot_write(
    session: Session,
    workspace: Workspace,
    viewer: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A read-only role is refused before the service layer runs."""
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
# Editing
# --------------------------------------------------------------------------------------


def test_edit_appends_an_immutable_version_and_returns_to_draft(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Editing the content creates a new version and parks the report for re-verification."""
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
    # The new version inherits the previous citation set so it stays publishable.
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
    """Changing `visibility` does not create a version, because the artifact is unchanged."""
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
    """A published report must be rolled back rather than rewritten in place."""
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
    """The request body is validated before the service is reached."""
    case = _seed(session, workspace)

    response = client.patch(
        f"{BASE}/{case.report_id}",
        json={"visibility": "SECRET"},
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------------------
# Verify, review, publish
# --------------------------------------------------------------------------------------


def test_verify_returns_and_stores_the_verdict(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """Running the Verifier over HTTP persists a passing verdict for the current version."""
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
    """`AUTO` has no withdrawal window, so the publish action releases it straight away."""
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
    """`AUTO_WITH_DELAY` enters the queue and the sweep leaves it there until due."""
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
    """The `MANUAL` path needs a recorded approval before the same action releases it."""
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
    """`INV-5` extra gate: a queued `MANUAL` report with no approval cannot be released."""
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
    """`request_changes` sends the report back and `reject` terminates it."""
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

    # A returned report can be edited again, which puts it back in the draft queue.
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
    """One blocked release is reported and does not abort the sweep."""
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
# Rollback
# --------------------------------------------------------------------------------------


def test_rollback_restores_a_historical_version_as_a_new_draft(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Rolling back appends a version rather than rewinding the history."""
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
    """Rolling back to the version already in force is a bad request."""
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
    """A version that was never recorded is a 404, not a silent no-op."""
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
    """A rejected report stays terminal; rollback cannot revive it."""
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
# Listing
# --------------------------------------------------------------------------------------


def test_list_supports_filters_and_pagination(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """Status filtering and page size are honoured, with a matching total."""
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
    """`page_size` follows the documented 1-100 bound."""
    response = client.get(
        f"{BASE}?page_size=101",
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
