"""Integration tests for the push API.

They cover the T13 surface end to end over HTTP: the delivery allowlist, one report delivery,
the delivery trail, and the retry driver — plus the refusal mapping and access control that
guard all of it. The default `push_delivery="offline"` transport is used throughout, so these
tests exercise the real route, service, and persistence stack without touching a network.
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
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """An administrator, who may both read every report and push."""
    return create_actor(session, username="push-admin", role="ADMIN")


@pytest.fixture()
def editor(session: Session) -> Actor:
    """An editor on the team scope, holding the other write role."""
    return create_actor(session, username="push-editor", role="EDITOR", data_scope="team")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """A read-only user, who must not be able to push at all."""
    return create_actor(session, username="push-viewer", role="VIEWER")


def _seed_failed_push(
    session: Session,
    *,
    report_id: int,
    target_id: int,
    version: int = 1,
    attempt: int = 1,
) -> ReportPush:
    """Plant a failed delivery row, standing in for a transport that refused the last try."""
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
# Allowlist
# --------------------------------------------------------------------------------------


def test_the_allowlist_lists_enabled_targets_only(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """A disabled destination is not offered to an operator."""
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
    """The channel filter narrows the list, and an unused channel yields nothing."""
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
# Delivery
# --------------------------------------------------------------------------------------


def test_a_delivery_is_recorded_and_visible_in_the_trail(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """One push writes one sent row and one audit entry, and the trail reports the counts."""
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
    """The same (report, version, target) triple is delivered at most once."""
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
    # Only the first delivery is audited: a replay never reaches the transport.
    assert audit_trail(session_factory).count(("report.push", "success")) == 1


def test_a_published_report_can_be_pushed(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """`published` is the other status a push is allowed from."""
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
    """Only an approved or published report may leave the building."""
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
    """Sensitive material needs a recorded human approval before it can go out."""
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
    """An unknown destination id is a bad request, not a delivery failure."""
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
    """The channel is not taken on trust: it has to agree with the allowlist row."""
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
    """A target that was never cleared for `PRIVATE` reports is refused."""
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
    """A missing report is answered as Problem Details rather than an internal error."""
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
    """The channel is a closed set, so anything else fails before the service is reached."""
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
    """Turning the feature off answers `503`, which tells a caller to come back later."""
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
# Retry driver
# --------------------------------------------------------------------------------------


def test_the_retry_driver_redelivers_a_failed_push(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """A failed row with budget left is redelivered by the sweep, on the same row."""
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
    """Nothing to retry is a successful, empty sweep."""
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
    """A row that already spent the retry budget is left alone rather than retried forever."""
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
    """The status filter is how an operator isolates the backlog."""
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
# Access control
# --------------------------------------------------------------------------------------


def test_a_read_only_role_cannot_push(
    session: Session,
    workspace: Workspace,
    viewer: Actor,
    client: TestClient,
) -> None:
    """Pushing is a write action, so a `VIEWER` is refused before any guard runs."""
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
    """No token means `401` on the action, the allowlist, the trail, and the sweep."""
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
    """The other write role is allowed too, provided the report itself is readable."""
    report = seed_pushable_report(session, workspace, status=APPROVED)
    target = workspace.add_push_target()

    response = client.post(
        f"{BASE}/{report.id}/push",
        json={"channel": "email", "target_id": target.id},
        headers=auth_headers(client, editor),
    )

    assert response.status_code == 200, response.text
    assert response.json()["delivered"] is True
