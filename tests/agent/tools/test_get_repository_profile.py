"""Tests for `get_repository_profile`: master data, ACL, and credential safety."""

from __future__ import annotations

from datetime import UTC, datetime

from support import Workspace

from open_source_intelligence.agent.tools.contracts import (
    TOOL_ACCESS_DENIED,
    TOOL_NOT_FOUND,
)
from open_source_intelligence.agent.tools.registry import build_default_registry

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _seed(workspace: Workspace):
    topic = workspace.add_topic()
    source = workspace.add_source(source_type="github")
    repository = workspace.add_repository(
        source_id=source.id,
        full_name="acme/rocket",
        stars=1500,
        forks=120,
        language="Rust",
        topics=["ai", "agents"],
    )
    workspace.link(topic.id, repository.id)
    run = workspace.add_agent_run(topic.id)
    return topic, repository, run


def test_returns_profile_with_provider_and_topics(workspace: Workspace) -> None:
    """The profile exposes provider, counters, and topics."""
    topic, repository, run = _seed(workspace)
    registry = build_default_registry()

    result = registry.invoke(
        "get_repository_profile",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {"repo_id": repository.id},
    )

    assert result.ok is True
    assert result.meta.result_count == 1
    profile = result.data
    assert profile is not None
    assert profile.repo_id == repository.id
    assert profile.provider == "github"
    assert profile.full_name == "acme/rocket"
    assert profile.language == "Rust"
    assert profile.topics == ["ai", "agents"]
    assert profile.stars == 1500


def test_profile_never_exposes_credentials(workspace: Workspace) -> None:
    """No credential-looking field is part of the profile payload."""
    topic, repository, run = _seed(workspace)
    registry = build_default_registry()

    result = registry.invoke(
        "get_repository_profile",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {"repo_id": repository.id},
    )

    assert result.data is not None
    dumped = result.data.model_dump()
    assert "credential_ref" not in dumped
    assert "base_url" not in dumped
    assert set(dumped) == {
        "repo_id",
        "provider",
        "full_name",
        "html_url",
        "description",
        "language",
        "license",
        "topics",
        "stars",
        "forks",
        "first_seen_at",
        "last_seen_at",
    }


def test_unauthorized_repository_is_denied(workspace: Workspace) -> None:
    """A repository outside the authorized topics is denied."""
    topic, repository, run = _seed(workspace)
    other = workspace.add_topic(name="Other")
    registry = build_default_registry()

    result = registry.invoke(
        "get_repository_profile",
        workspace.context(topic.id, agent_run_id=run.id, authorized=[other.id]),
        workspace.session,
        {"repo_id": repository.id},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == TOOL_ACCESS_DENIED


def test_missing_repository_returns_not_found(workspace: Workspace) -> None:
    """Unknown repositories report TOOL_NOT_FOUND."""
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    registry = build_default_registry()

    result = registry.invoke(
        "get_repository_profile",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {"repo_id": 987654},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == TOOL_NOT_FOUND
