"""Integration tests for the audit log endpoint.

`REQUIREMENTS.md` FR-D-05 requires an audit API, and FR-S-02 requires that logins, configuration
changes, reviews and publishes are traceable. These tests cover the read side of that: only an
administrator may read the trail, the filters select what they say they select, and two fields are
deliberately absent from the response.
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

#: A value that only exists in `metadata_json`. It must not reach the response: the field is
#: free-form and its allowed keys were never defined, so an egress path should not carry it.
UNPUBLISHED_METADATA = "internal-context-that-was-never-contracted"


@dataclass(frozen=True, slots=True)
class Trail:
    """The seeded audit rows, identified by the action that produced them."""

    actor_id: int
    report_read_ids: tuple[int, ...]
    auth_csrf_id: int

    @property
    def seeded_ids(self) -> set[int]:
        """Every id the fixture wrote, so an assertion can ignore the login event."""
        return {*self.report_read_ids, self.auth_csrf_id}


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """An administrator: the only role that may read the audit trail."""
    return create_actor(session, username="admin", role="ADMIN")


@pytest.fixture()
def editor(session: Session) -> Actor:
    """An editor, who may not read the trail even though they may write reports."""
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
    """Insert one audit row and return it after flushing its id."""
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
    """Seed a small trail: two denied report reads and one CSRF denial."""
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
    """The trail is not public: it names every refused request on the platform."""
    assert client.get(AUDIT).status_code == 401


def test_an_editor_is_refused_and_the_refusal_is_recorded(
    client: TestClient,
    editor: Actor,
    trail: Trail,
    session: Session,
) -> None:
    """Reading the trail is itself a privilege, and a refusal is an audit event like any other.

    This is deliberately the same shape the report ACL uses: `403` plus a `denied` row. The
    check reads the row back through the application's own table rather than a counter.
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
    """The trail is ordered by id descending, and the administrator's own login is on it.

    That the login appears at all is the point: the endpoint reads the application's real trail
    rather than a fixture-only view, so the request that authenticates the caller is itself an
    event. The ordering assertion is therefore about the seeded rows' relative order rather than
    about absolute positions.
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
    """`metadata_json` stays inside the process until its allowed keys are defined.

    The assertion is on the raw body rather than on the parsed keys, so adding the field later
    without deciding what may go in it fails here instead of shipping silently.
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
    """One column holds IPv4 and IPv6; the pair of helpers is what makes both readable."""
    response = client.get(AUDIT, headers=auth_headers(client, admin))
    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["items"]}
    assert by_id[trail.report_read_ids[0]]["ip_address"] == "203.0.113.7"
    assert by_id[trail.report_read_ids[1]]["ip_address"] == "2001:db8::1"
    # A request-scoped denial has no client address, and `null` is the honest answer.
    assert by_id[trail.auth_csrf_id]["ip_address"] is None


def test_filters_select_what_they_say(
    client: TestClient,
    admin: Actor,
    trail: Trail,
) -> None:
    """Each filter narrows both the page and the total."""
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

    # The caller's own login is a `success` event, which is what makes `result=success` non-empty
    # rather than a filter that silently matches nothing.
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
    """ "No rows" and "that outcome cannot exist" are different findings."""
    response = client.get(AUDIT, headers=auth_headers(client, admin), params={"result": "maybe"})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_the_time_window_is_half_open(
    client: TestClient,
    admin: Actor,
    trail: Trail,
) -> None:
    """`created_before` is exclusive, so a row is never in two adjacent windows at once."""
    headers = auth_headers(client, admin)
    everything = client.get(AUDIT, headers=headers).json()
    newest = next(item for item in everything["items"] if item["id"] == trail.auth_csrf_id)

    # Ending exactly at the newest row excludes it: that is what "half open" buys, and it is the
    # property a caller paging forward through time depends on.
    excludes = client.get(
        AUDIT,
        headers=headers,
        params={"created_before": newest["created_at"]},
    ).json()
    assert trail.auth_csrf_id not in {item["id"] for item in excludes["items"]}

    # A naive timestamp is refused at the boundary rather than reaching the query builder.
    naive = client.get(AUDIT, headers=headers, params={"created_after": "2026-10-15T09:00:00"})
    assert naive.status_code == 422
    assert "timezone offset" in naive.text


def test_page_size_is_bounded(
    client: TestClient,
    admin: Actor,
    trail: Trail,
) -> None:
    """The platform-wide 1-100 page bound applies to the trail too."""
    response = client.get(
        AUDIT,
        headers=auth_headers(client, admin),
        params={"page_size": MAX_PAGE_SIZE + 1},
    )
    assert response.status_code == 422
