"""Tests for `search_prior_reports`: ACL, time window, and result shaping."""

from __future__ import annotations

from datetime import timedelta

from support import NOW, Workspace

from open_source_intelligence.agent.tools.contracts import TOOL_ACCESS_DENIED
from open_source_intelligence.agent.tools.registry import build_default_registry

BEFORE = NOW + timedelta(hours=1)


def _seed(workspace: Workspace):
    topic = workspace.add_topic()
    source = workspace.add_source()
    repository = workspace.add_repository(source_id=source.id)
    workspace.link(topic.id, repository.id)
    run = workspace.add_agent_run(topic.id)
    return topic, repository, run


def test_returns_published_prior_reports(workspace: Workspace) -> None:
    """Prior published reports are summarized with their citation refs."""
    topic, repository, run = _seed(workspace)
    workspace.add_report(
        topic_id=topic.id,
        agent_run_id=run.id,
        title="Week 40",
        content="Stars grew strongly across the tracked agents.",
        generated_at=NOW - timedelta(days=7),
        repository_id=repository.id,
        source_refs=["repo:1#stars", "repo:1#forks"],
    )
    registry = build_default_registry()

    result = registry.invoke(
        "search_prior_reports",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {"topic_id": topic.id, "before": BEFORE},
    )

    assert result.ok is True
    assert result.meta.result_count == 1
    summary = result.data[0]
    assert summary.title == "Week 40"
    assert summary.section_key == "summary"
    assert "Stars grew" in summary.summary
    assert sorted(summary.source_refs) == ["repo:1#forks", "repo:1#stars"]


def test_draft_reports_are_excluded(workspace: Workspace) -> None:
    """Only published reports count as prior reports."""
    topic, repository, run = _seed(workspace)
    workspace.add_report(
        topic_id=topic.id,
        agent_run_id=run.id,
        title="Draft",
        status="draft",
        generated_at=NOW - timedelta(days=1),
    )
    registry = build_default_registry()

    result = registry.invoke(
        "search_prior_reports",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {"topic_id": topic.id, "before": BEFORE},
    )

    assert result.ok is True
    assert result.meta.result_count == 0


def test_reports_not_before_the_cutoff_are_excluded(workspace: Workspace) -> None:
    """The `before` bound excludes newer reports."""
    topic, repository, run = _seed(workspace)
    workspace.add_report(
        topic_id=topic.id,
        agent_run_id=run.id,
        title="Old",
        generated_at=NOW - timedelta(days=30),
    )
    workspace.add_report(
        topic_id=topic.id,
        agent_run_id=run.id,
        title="New",
        generated_at=NOW + timedelta(days=2),
    )
    registry = build_default_registry()

    result = registry.invoke(
        "search_prior_reports",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {"topic_id": topic.id, "before": BEFORE},
    )

    assert result.ok is True
    assert [summary.title for summary in result.data] == ["Old"]


def test_unauthorized_topic_is_denied_without_leaking(workspace: Workspace) -> None:
    """An out-of-scope topic is denied and returns no summaries or counts."""
    topic, repository, run = _seed(workspace)
    other = workspace.add_topic(name="Other")
    workspace.add_report(
        topic_id=other.id,
        agent_run_id=run.id,
        title="Secret",
        generated_at=NOW - timedelta(days=3),
    )
    registry = build_default_registry()

    result = registry.invoke(
        "search_prior_reports",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {"topic_id": other.id, "before": BEFORE},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == TOOL_ACCESS_DENIED
    assert result.data is None
    assert result.meta.result_count == 0
    assert "Secret" not in result.error.message


def test_repository_filter_and_limit(workspace: Workspace) -> None:
    """A repo filter narrows results and the limit sets the truncated flag."""
    topic, repository, run = _seed(workspace)
    other_repository = workspace.add_repository(
        source_id=repository.source_id,
        provider_repo_id="2",
    )
    for index in range(3):
        workspace.add_report(
            topic_id=topic.id,
            agent_run_id=run.id,
            title=f"Report {index}",
            generated_at=NOW - timedelta(days=index + 1),
            repository_id=repository.id,
            source_refs=[f"repo:{repository.id}#stars"],
        )
    workspace.add_report(
        topic_id=topic.id,
        agent_run_id=run.id,
        title="Unrelated",
        generated_at=NOW - timedelta(days=1),
        repository_id=other_repository.id,
    )
    registry = build_default_registry()

    result = registry.invoke(
        "search_prior_reports",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {"topic_id": topic.id, "repo_id": repository.id, "before": BEFORE, "limit": 2},
    )

    assert result.ok is True
    assert result.meta.result_count == 2
    assert result.meta.truncated is True
    assert all("Unrelated" != summary.title for summary in result.data)


def test_two_reports_with_the_same_text_are_both_returned(workspace: Workspace) -> None:
    """Relevance ranking must key on position, not on the text it ranked.

    Two reports can share a title and an identical opening paragraph. A `{text: summary}` lookup
    collapsed them into one entry and then emitted it twice, so a baseline silently went missing
    while the result count still looked right.
    """
    topic, repository, run = _seed(workspace)
    for offset in (7, 14):
        workspace.add_report(
            topic_id=topic.id,
            agent_run_id=run.id,
            title="Weekly wrap",
            content="Stars grew strongly across the tracked agents.",
            generated_at=NOW - timedelta(days=offset),
            repository_id=repository.id,
            source_refs=["repo:1#stars"],
        )
    registry = build_default_registry()

    result = registry.invoke(
        "search_prior_reports",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {"topic_id": topic.id, "before": BEFORE, "query": "stars growth"},
    )

    assert result.ok is True
    assert result.meta.result_count == 2
    assert len({summary.report_id for summary in result.data}) == 2
