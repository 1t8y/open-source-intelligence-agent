"""Fixtures for Verifier and publishing tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from reports_support import AUTO_WITH_DELAY, SeededReport, build_report
from sqlalchemy.orm import Session
from support import Workspace, build_settings

from open_source_intelligence.reports import publishing, verification


@pytest.fixture(autouse=True)
def publish_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the publish stack a deterministic `Settings` without requiring a real `.env`.

    `verification.py` and `publishing.py` fall back to `get_settings()` when no settings are
    passed, which is correct in production. Tests scope the environment here so the real
    fallback path is still exercised, while explicit overrides keep working.
    """
    settings = build_settings()
    monkeypatch.setattr(verification, "get_settings", lambda: settings)
    monkeypatch.setattr(publishing, "get_settings", lambda: settings)


@pytest.fixture
def report_case(session: Session, workspace: Workspace) -> Iterator[SeededReport]:
    """Provide a generated report whose topic uses `AUTO_WITH_DELAY`."""
    yield build_report(session, workspace, publish_mode=AUTO_WITH_DELAY)
