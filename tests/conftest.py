"""Shared in-memory database fixtures for the whole test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace, build_session_factory


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
