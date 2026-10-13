"""Fixtures for Verifier and publishing tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from reports_support import AUTO_WITH_DELAY, SeededReport, build_report
from sqlalchemy.orm import Session
from support import Workspace


@pytest.fixture
def report_case(session: Session, workspace: Workspace) -> Iterator[SeededReport]:
    """Provide a generated report whose topic uses `AUTO_WITH_DELAY`."""
    yield build_report(session, workspace, publish_mode=AUTO_WITH_DELAY)
