"""Tests for `get_repository_content`: cleaning, truncation, and untrusted flag."""

from __future__ import annotations

from datetime import UTC, datetime

from support import Workspace

from open_source_intelligence.agent.tools.contracts import (
    MAX_CONTENT_CHARS,
    TOOL_ACCESS_DENIED,
    TOOL_NOT_FOUND,
)
from open_source_intelligence.agent.tools.registry import build_default_registry

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _seed(workspace: Workspace):
    topic = workspace.add_topic()
    source = workspace.add_source()
    repository = workspace.add_repository(source_id=source.id)
    workspace.link(topic.id, repository.id)
    run = workspace.add_agent_run(topic.id)
    return topic, repository, run


def test_returns_cleaned_untrusted_content(workspace: Workspace) -> None:
    """Script tags and control characters are stripped before returning text."""
    topic, repository, run = _seed(workspace)
    raw = "# Rocket\n<script>alert('x')</script>\n\n\n\nA tool\x07 for agents"
    workspace.add_content(repository.id, content=raw, source_url="https://example.com/readme")
    registry = build_default_registry()

    result = registry.invoke(
        "get_repository_content",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {"repo_id": repository.id},
    )

    assert result.ok is True
    snippet = result.data
    assert snippet is not None
    assert "alert" not in snippet.content
    assert "<script>" not in snippet.content
    assert "\x07" not in snippet.content
    assert snippet.untrusted is True
    assert snippet.truncated is False
    assert snippet.source_url == "https://example.com/readme"
    assert len(snippet.content_hash) == 64


def test_long_content_is_truncated_and_flagged(workspace: Workspace) -> None:
    """Content beyond max_chars is cut exactly at the bound and flagged."""
    topic, repository, run = _seed(workspace)
    workspace.add_content(repository.id, content="a" * MAX_CONTENT_CHARS)
    registry = build_default_registry()

    result = registry.invoke(
        "get_repository_content",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {"repo_id": repository.id, "max_chars": 500},
    )

    assert result.ok is True
    assert result.meta.truncated is True
    snippet = result.data
    assert snippet is not None
    assert len(snippet.content) == 500
    assert snippet.truncated is True


def test_short_content_is_not_truncated(workspace: Workspace) -> None:
    """Content under the bound is returned whole with truncated=False."""
    topic, repository, run = _seed(workspace)
    workspace.add_content(repository.id, content="short readme")
    registry = build_default_registry()

    result = registry.invoke(
        "get_repository_content",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {"repo_id": repository.id},
    )

    assert result.ok is True
    assert result.meta.truncated is False
    assert result.data is not None and result.data.content == "short readme"


def test_missing_content_returns_not_found(workspace: Workspace) -> None:
    """A repository without cached content reports TOOL_NOT_FOUND."""
    topic, repository, run = _seed(workspace)
    registry = build_default_registry()

    result = registry.invoke(
        "get_repository_content",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {"repo_id": repository.id},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == TOOL_NOT_FOUND


def test_unauthorized_repository_is_denied(workspace: Workspace) -> None:
    """Content of an unauthorized repository is not readable."""
    topic, repository, run = _seed(workspace)
    other = workspace.add_topic(name="Other")
    workspace.add_content(repository.id, content="secret readme")
    registry = build_default_registry()

    result = registry.invoke(
        "get_repository_content",
        workspace.context(topic.id, agent_run_id=run.id, authorized=[other.id]),
        workspace.session,
        {"repo_id": repository.id},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == TOOL_ACCESS_DENIED
    assert result.data is None
