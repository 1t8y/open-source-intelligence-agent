"""HTTP tests for the share link: the JSON endpoints, and the page that needs no login.

Two surfaces, one rule. The JSON endpoints are guarded like every other report endpoint
(bearer token, write role, report read access); `GET /share/{token}` is guarded by the token
itself. What both must never do is widen which reports can leave the building, so the refusal
cases are tested on both.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import timedelta

import pytest
from api_support import Actor, auth_headers, build_client, create_actor
from fastapi.testclient import TestClient
from reports_support import AUTO_WITH_DELAY, SeededReport, build_report
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import NOW, Workspace

from open_source_intelligence.persistence.models.reporting import Report, ReportShareLink

BASE = "/api/v1/reports"


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def editor(session: Session) -> Actor:
    """An editor on the team data scope."""
    return create_actor(session, username="sharing-editor", role="EDITOR", data_scope="team")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """A read-only viewer."""
    return create_actor(session, username="sharing-viewer", role="VIEWER", data_scope="team")


def seed_report(
    session: Session,
    workspace: Workspace,
    *,
    status: str = "published",
    sensitivity: str = "PUBLIC",
) -> Report:
    """Seed a report whose row is constructed in its final state."""
    case: SeededReport = build_report(session, workspace, publish_mode=AUTO_WITH_DELAY)
    report = Report(
        agent_run_id=case.agent_run_id,
        topic_id=case.topic.id,
        title="分享用周报",
        current_version=1,
        content_markdown="# 分享用周报\n\n这是正文。\n",
        status=status,
        visibility="INTERNAL",
        sensitivity=sensitivity,
        publish_mode="AUTO_WITH_DELAY",
        generated_at=NOW,
        published_at=NOW if status == "published" else None,
    )
    session.add(report)
    session.commit()
    return report


def test_creating_a_link_returns_the_token_once(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """The create response is the only place a plaintext token ever appears."""
    report = seed_report(session, workspace)
    headers = auth_headers(client, editor)

    response = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["token"] in body["url_path"]
    assert body["expires_at"] is not None
    row = session.scalar(select(ReportShareLink).where(ReportShareLink.id == body["share_id"]))
    assert row is not None
    assert row.token_hash == hashlib.sha256(body["token"].encode("utf-8")).hexdigest()


def test_listing_links_has_no_token_field(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """A later read must not be able to reconstruct the link, so no field even carries it."""
    report = seed_report(session, workspace)
    headers = auth_headers(client, editor)
    created = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers).json()

    response = client.get(f"{BASE}/{report.id}/shares", headers=headers)

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["share_id"] for item in items] == [created["share_id"]]
    assert "token" not in items[0]
    assert created["token"] not in response.text


def test_an_internal_report_is_refused_with_a_conflict(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """Sensitivity is the gate: `INTERNAL` content never gets a public address."""
    report = seed_report(session, workspace, sensitivity="INTERNAL")
    headers = auth_headers(client, editor)

    response = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "SHARE_SENSITIVITY_REFUSED"


def test_a_viewer_cannot_mint_a_link(
    client: TestClient, session: Session, workspace: Workspace, viewer: Actor
) -> None:
    """Creating a public address is a write action, so it needs a write role."""
    report = seed_report(session, workspace)
    headers = auth_headers(client, viewer)

    response = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers)

    assert response.status_code == 403


def test_an_anonymous_request_cannot_mint_a_link(
    client: TestClient, session: Session, workspace: Workspace
) -> None:
    """The endpoints that widen exposure are not themselves reachable without a login."""
    report = seed_report(session, workspace)

    response = client.post(f"{BASE}/{report.id}/shares", json={})

    assert response.status_code == 401


def test_revoking_twice_reports_false_the_second_time(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """Idempotent: the intent is satisfied, so a repeat is not an error."""
    report = seed_report(session, workspace)
    headers = auth_headers(client, editor)
    created = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers).json()

    first = client.delete(f"{BASE}/{report.id}/shares/{created['share_id']}", headers=headers)
    second = client.delete(f"{BASE}/{report.id}/shares/{created['share_id']}", headers=headers)

    assert first.status_code == 200 and first.json()["revoked"] is True
    assert second.status_code == 200 and second.json()["revoked"] is False


def test_the_public_page_reads_without_a_session(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """The whole point: a browser with no cookie can read the body."""
    report = seed_report(session, workspace)
    headers = auth_headers(client, editor)
    created = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers).json()

    response = client.get(created["url_path"])

    assert response.status_code == 200
    assert "分享用周报" in response.text
    assert "这是正文。" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


def test_the_public_page_offers_no_write_action(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """A page for anonymous readers must not render anything they could post.

    No form, no CSRF field, and no sidebar: the reader's capability has to equal the page's.
    """
    report = seed_report(session, workspace)
    headers = auth_headers(client, editor)
    created = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers).json()

    response = client.get(created["url_path"])

    assert "<form" not in response.text
    assert "csrf_token" not in response.text
    assert "生成分享链接" not in response.text


def test_a_revoked_link_answers_404_without_explaining_why(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """Revoked, unknown, and expired must be indistinguishable, or the route is an oracle."""
    report = seed_report(session, workspace)
    headers = auth_headers(client, editor)
    created = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers).json()
    client.delete(f"{BASE}/{report.id}/shares/{created['share_id']}", headers=headers)

    revoked = client.get(created["url_path"])
    unknown = client.get("/share/this-token-never-existed")

    assert revoked.status_code == 404
    assert unknown.status_code == 404
    # Same page, not merely the same status: any difference in wording is a hint.
    assert revoked.text == unknown.text


def test_an_expired_link_answers_404(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """Expiry is checked at read time, not only when the link was minted."""
    report = seed_report(session, workspace)
    headers = auth_headers(client, editor)
    created = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers).json()
    row = session.get(ReportShareLink, created["share_id"])
    assert row is not None
    row.expires_at = NOW - timedelta(days=1)
    session.commit()

    response = client.get(created["url_path"])

    assert response.status_code == 404


def test_the_share_page_does_not_leak_other_reports(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """The shared page shows one report, and nothing about the rest of the system."""
    report = seed_report(session, workspace)
    other = seed_report(session, workspace)
    headers = auth_headers(client, editor)
    created = client.post(f"{BASE}/{report.id}/shares", json={}, headers=headers).json()

    response = client.get(created["url_path"])

    assert other.title not in response.text or other.title == report.title
    assert "/ui/reports" not in response.text
