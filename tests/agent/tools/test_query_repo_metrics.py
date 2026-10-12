"""Tests for `query_repo_metrics`: windowing, ACL, and read-only idempotency."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import func, select
from support import Workspace

from open_source_intelligence.agent.tools.contracts import (
    TOOL_ACCESS_DENIED,
    TOOL_INVALID_ARGUMENT,
    TOOL_NOT_FOUND,
)
from open_source_intelligence.agent.tools.registry import build_default_registry
from open_source_intelligence.persistence.models.agent import ToolCall
from open_source_intelligence.persistence.models.intelligence import RepoSnapshot

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _seed(workspace: Workspace):
    topic = workspace.add_topic()
    source = workspace.add_source()
    repository = workspace.add_repository(source_id=source.id)
    workspace.link(topic.id, repository.id)
    run = workspace.add_agent_run(topic.id)
    return topic, repository, run


def test_returns_only_points_inside_the_window(workspace: Workspace) -> None:
    """Out-of-window snapshots are excluded, in-window ones are ordered by date."""
    topic, repository, run = _seed(workspace)
    workspace.add_snapshot(repository.id, date(2026, 10, 5), stars=10, forks=1, stars_delta=1)
    workspace.add_snapshot(repository.id, date(2026, 10, 6), stars=12, forks=2, stars_delta=2)
    workspace.add_snapshot(repository.id, date(2026, 10, 20), stars=99, forks=9)
    registry = build_default_registry()

    result = registry.invoke(
        "query_repo_metrics",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {
            "repo_id": repository.id,
            "start": datetime(2026, 10, 1, tzinfo=UTC),
            "end": datetime(2026, 10, 10, tzinfo=UTC),
        },
    )

    assert result.ok is True
    assert result.meta.result_count == 2
    assert result.meta.truncated is False
    assert result.data is not None
    assert [point.snapshot_date for point in result.data] == [
        date(2026, 10, 5),
        date(2026, 10, 6),
    ]
    assert result.data[1].stars_delta == 2


def test_repository_outside_authorized_topics_is_denied(workspace: Workspace) -> None:
    """A repository linked only to another topic is not readable."""
    topic, repository, run = _seed(workspace)
    other = workspace.add_topic(name="Other")
    workspace.add_snapshot(repository.id, date(2026, 10, 5), stars=10, forks=1)
    registry = build_default_registry()

    result = registry.invoke(
        "query_repo_metrics",
        workspace.context(topic.id, agent_run_id=run.id, authorized=[other.id]),
        workspace.session,
        {
            "repo_id": repository.id,
            "start": datetime(2026, 10, 1, tzinfo=UTC),
            "end": datetime(2026, 10, 10, tzinfo=UTC),
        },
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == TOOL_ACCESS_DENIED
    assert result.data is None


def test_missing_repository_returns_not_found(workspace: Workspace) -> None:
    """A repository that does not exist reports the dedicated code."""
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    registry = build_default_registry()

    result = registry.invoke(
        "query_repo_metrics",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {
            "repo_id": 424242,
            "start": datetime(2026, 10, 1, tzinfo=UTC),
            "end": datetime(2026, 10, 10, tzinfo=UTC),
        },
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == TOOL_NOT_FOUND


def test_over_long_window_is_rejected_before_query(workspace: Workspace) -> None:
    """The 366-day guard rejects the call instead of silently trimming."""
    topic, repository, run = _seed(workspace)
    registry = build_default_registry()

    result = registry.invoke(
        "query_repo_metrics",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {
            "repo_id": repository.id,
            "start": datetime(2020, 1, 1, tzinfo=UTC),
            "end": datetime(2026, 10, 1, tzinfo=UTC),
        },
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == TOOL_INVALID_ARGUMENT


def test_read_only_calls_are_idempotent(workspace: Workspace) -> None:
    """Repeating a read-only call returns identical data and writes no domain rows."""
    topic, repository, run = _seed(workspace)
    workspace.add_snapshot(repository.id, date(2026, 10, 5), stars=10, forks=1)
    registry = build_default_registry()
    ctx = workspace.context(topic.id, agent_run_id=run.id)
    arguments = {
        "repo_id": repository.id,
        "start": datetime(2026, 10, 1, tzinfo=UTC),
        "end": datetime(2026, 10, 10, tzinfo=UTC),
    }
    snapshots_before = workspace.session.scalar(select(func.count()).select_from(RepoSnapshot))

    first = registry.invoke("query_repo_metrics", ctx, workspace.session, arguments)
    second = registry.invoke("query_repo_metrics", ctx, workspace.session, arguments)

    assert first.data == second.data
    assert workspace.session.scalar(select(func.count()).select_from(RepoSnapshot)) == (
        snapshots_before
    )
    assert workspace.session.scalar(select(func.count()).select_from(ToolCall)) == 2
