"""Shared in-memory database fixtures for the whole test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace, build_session_factory, build_settings

from open_source_intelligence.notifications import service as notifications_service
from open_source_intelligence.reports import publishing, verification


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    """Provide a shared in-memory database factory."""
    return build_session_factory()


@pytest.fixture()
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Provide a session bound to the shared database."""
    with session_factory() as active:
        yield active


@pytest.fixture()
def workspace(session: Session) -> Workspace:
    """Provide the seeding helper bound to the active session."""
    return Workspace(session)


@pytest.fixture(autouse=True)
def publish_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the publish stack deterministic `Settings` without requiring a real `.env`.

    `verification`, `publishing`, and the push service fall back to `get_settings()` when no
    settings are passed, which is correct in production. Tests scope the environment here so
    that real fallback path is still exercised while remaining independent of a developer's
    `.env`. Passing settings explicitly keeps working and takes precedence.
    """
    settings = build_settings()
    monkeypatch.setattr(verification, "get_settings", lambda: settings)
    monkeypatch.setattr(publishing, "get_settings", lambda: settings)
    monkeypatch.setattr(notifications_service, "get_settings", lambda: settings)
