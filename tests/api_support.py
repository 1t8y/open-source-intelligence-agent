"""App, actor, and report helpers for the report API and admin UI tests.

The tests seed through one session and then assert through the API/UI or through a fresh
session, because the shared in-memory database lends a single connection to every session:
leaving a seeding transaction open would block the application's own session.

The module sits at the top of `tests/` (next to `support.py`) so both `tests/api` and
`tests/web` can import it as a plain top-level module.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from itertools import count

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import build_settings

from open_source_intelligence.api.app import create_app
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.identity import Role, User, UserRole
from open_source_intelligence.persistence.models.reporting import Report, ReportSource
from open_source_intelligence.security.dependencies import get_db_session
from open_source_intelligence.security.passwords import hash_password
from open_source_intelligence.web.security import CSRF_COOKIE

LOGIN_PATH = "/ui/login"

PASSWORD = "correct horse battery staple"

#: `roles.id` is a plain primary key, so the three standard roles use fixed ids.
_ROLE_IDS = {"ADMIN": 1, "EDITOR": 2, "VIEWER": 3}

#: `support.Workspace` inserts its own user as id 1, so actors start well above that range.
_ACTOR_IDS = count(1001)


@dataclass(frozen=True, slots=True)
class Actor:
    """One seeded user holding exactly one role."""

    user_id: int
    username: str
    role: str
    data_scope: str


def create_actor(
    session: Session,
    *,
    username: str,
    role: str,
    data_scope: str = "personal",
) -> Actor:
    """Create one user holding a single standard role."""
    role_row = session.scalar(select(Role).where(Role.code == role))
    if role_row is None:
        role_row = Role(id=_ROLE_IDS[role], code=role, name=role.title())
        session.add(role_row)
        session.flush()
    user = User(
        id=next(_ACTOR_IDS),
        username=username,
        password_hash=hash_password(PASSWORD),
        display_name=username,
        data_scope=data_scope,
    )
    session.add(user)
    session.flush()
    session.add(UserRole(user_id=user.id, role_id=role_row.id))
    session.commit()
    return Actor(user_id=user.id, username=username, role=role, data_scope=data_scope)


def build_client(session_factory: sessionmaker[Session]) -> TestClient:
    """Build a client whose dependency graph uses the shared test database."""
    app = create_app(build_settings())

    def override_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session
    return TestClient(app)


def auth_headers(client: TestClient, actor: Actor) -> dict[str, str]:
    """Log the actor in and return a bearer header."""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": actor.username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def configure_report(
    session: Session,
    *,
    report_id: int,
    visibility: str | None = None,
    owner_id: int | None = None,
    status: str | None = None,
    publish_mode: str | None = None,
    publish_at: datetime | None = None,
) -> Report:
    """Set the fields a test varies and commit so the API can see them."""
    report = session.get(Report, report_id)
    assert report is not None
    if visibility is not None:
        report.visibility = visibility
    report.owner_id = owner_id
    if status is not None:
        report.status = status
    if publish_mode is not None:
        report.publish_mode = publish_mode
    if publish_at is not None:
        report.publish_at = publish_at
    session.commit()
    return report


def load_report(session_factory: sessionmaker[Session], report_id: int) -> Report:
    """Read one report through a short-lived session."""
    with session_factory() as session:
        report = session.get(Report, report_id)
        assert report is not None
        return report


def source_versions(session_factory: sessionmaker[Session], report_id: int) -> set[int]:
    """Return the versions that currently have citation rows."""
    with session_factory() as session:
        return {
            int(row)
            for row in session.scalars(
                select(ReportSource.version).where(ReportSource.report_id == report_id)
            ).all()
        }


def audit_trail(session_factory: sessionmaker[Session]) -> list[tuple[str, str]]:
    """Return `(action, result)` pairs in insertion order."""
    with session_factory() as session:
        rows = session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
        return [(row.action, row.result) for row in rows]


# --------------------------------------------------------------------------------------
# Admin UI helpers
# --------------------------------------------------------------------------------------


def csrf_value(client: TestClient) -> str:
    """Return the CSRF value the browser currently holds, or fail loudly."""
    value = client.cookies.get(CSRF_COOKIE)
    assert value, "no CSRF cookie: render a page through the UI first"
    return value


def prime_csrf(client: TestClient, path: str = LOGIN_PATH) -> str:
    """Render one page so the server issues a CSRF cookie, then return its value."""
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 200, response.text
    return csrf_value(client)


def web_login(
    client: TestClient,
    actor: Actor,
    *,
    password: str = PASSWORD,
    next_url: str = "/ui/reports",
) -> httpx.Response:
    """Sign the actor in through the UI form and keep the resulting session cookie."""
    token = prime_csrf(client)
    return client.post(
        LOGIN_PATH,
        data={
            "csrf_token": token,
            "username": actor.username,
            "password": password,
            "next": next_url,
        },
        follow_redirects=False,
    )


def web_logout(client: TestClient) -> httpx.Response:
    """Submit the logout form with the current CSRF value."""
    return client.post(
        "/ui/logout",
        data={"csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
