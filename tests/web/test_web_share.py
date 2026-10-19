"""UI tests for the share card: the button appears only where the service would accept it.

The interesting failures here are "menu can offer, click gets 403" ones. The card decides
whether to render the button from the same two constants (`SHAREABLE_SENSITIVITY`,
`SHAREABLE_STATUS`) the service refuses on, and this file checks both sides of that promise:
what is rendered, and what actually happens when the form is posted.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from api_support import Actor, build_client, create_actor, csrf_value, web_login
from fastapi.testclient import TestClient
from reports_support import AUTO_WITH_DELAY, SeededReport, build_report
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import NOW, Workspace

from open_source_intelligence.persistence.models.reporting import Report, ReportShareLink


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def editor(session: Session) -> Actor:
    """An editor on the team data scope."""
    return create_actor(session, username="share-ui-editor", role="EDITOR", data_scope="team")


def seed_report(
    session: Session,
    workspace: Workspace,
    *,
    status: str = "published",
    sensitivity: str = "PUBLIC",
) -> Report:
    """Seed a report in a chosen publishing state and sensitivity."""
    case: SeededReport = build_report(session, workspace, publish_mode=AUTO_WITH_DELAY)
    report = Report(
        agent_run_id=case.agent_run_id,
        topic_id=case.topic.id,
        title="界面分享用周报",
        current_version=1,
        content_markdown="# 界面分享用周报\n\n正文。\n",
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


def test_the_button_is_offered_for_a_public_published_report(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """Where the service would accept, the card offers the action."""
    report = seed_report(session, workspace)
    web_login(client, editor)

    response = client.get(f"/ui/reports/{report.id}")

    assert response.status_code == 200
    assert "生成分享链接" in response.text


@pytest.mark.parametrize(
    ("status", "sensitivity"),
    [("published", "INTERNAL"), ("published", "CONFIDENTIAL"), ("draft", "PUBLIC")],
)
def test_a_disallowed_report_explains_itself_instead_of_hiding(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    editor: Actor,
    status: str,
    sensitivity: str,
) -> None:
    """A missing button with no explanation reads like a broken page.

    The refusal text has to name the actual reason, because the two reasons call for different
    fixes: change the sensitivity, or publish the report.
    """
    report = seed_report(session, workspace, status=status, sensitivity=sensitivity)
    web_login(client, editor)

    response = client.get(f"/ui/reports/{report.id}")

    assert response.status_code == 200
    assert "当前不能生成分享链接" in response.text
    assert "生成分享链接</button>" not in response.text


def test_posting_the_form_renders_the_link_once(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """The response carries the plaintext token; the database does not."""
    report = seed_report(session, workspace)
    web_login(client, editor)

    response = client.post(
        f"/ui/reports/{report.id}/share",
        data={"csrf_token": csrf_value(client)},
    )

    assert response.status_code == 200
    assert "/share/" in response.text
    row = session.scalar(select(ReportShareLink).where(ReportShareLink.report_id == report.id))
    assert row is not None
    # The rendered page contains the token; the row must not.
    token = response.text.split("/share/")[1].split('"')[0]
    assert row.token_hash != token
    assert token not in str(row.__dict__)


def test_posting_without_csrf_creates_nothing(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """The write path keeps the same CSRF guard as every other action."""
    report = seed_report(session, workspace)
    web_login(client, editor)

    response = client.post(f"/ui/reports/{report.id}/share", data={"csrf_token": "wrong"})

    assert response.status_code == 403
    assert session.scalars(select(ReportShareLink)).all() == []


def test_revoking_from_the_panel_stops_the_public_page(
    client: TestClient, session: Session, workspace: Workspace, editor: Actor
) -> None:
    """Revoke is the whole safety valve, so it is tested end to end: card → page → 404."""
    report = seed_report(session, workspace)
    web_login(client, editor)
    created = client.post(
        f"/ui/reports/{report.id}/share",
        data={"csrf_token": csrf_value(client)},
    )
    token = created.text.split("/share/")[1].split('"')[0]

    assert client.get(f"/share/{token}").status_code == 200

    row = session.scalar(select(ReportShareLink).where(ReportShareLink.report_id == report.id))
    assert row is not None
    revoked = client.post(
        f"/ui/reports/{report.id}/share/{row.id}/revoke",
        data={"csrf_token": csrf_value(client)},
    )

    assert revoked.status_code == 200
    assert "链接已撤回" in revoked.text
    assert client.get(f"/share/{token}").status_code == 404
