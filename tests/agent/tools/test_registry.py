"""Registry tests: whitelist, ACL, budget, read-only, and error mapping."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from support import ALL_TOOLS, Workspace

from open_source_intelligence.agent.tools.context import ToolContext
from open_source_intelligence.agent.tools.contracts import (
    TOOL_ACCESS_DENIED,
    TOOL_BUDGET_EXCEEDED,
    TOOL_INTERNAL_ERROR,
    TOOL_INVALID_ARGUMENT,
    GetRepositoryProfileInput,
)
from open_source_intelligence.agent.tools.outcome import ToolOutcome
from open_source_intelligence.agent.tools.registry import (
    WHITELIST,
    ToolRegistry,
    ToolSpec,
    build_default_registry,
)
from open_source_intelligence.persistence.models.agent import ToolCall
from open_source_intelligence.persistence.models.identity import User


def _count_tool_calls(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(ToolCall)) or 0


def test_registry_exposes_the_contract_whitelist() -> None:
    """The registry holds exactly the documented tools."""
    registry = build_default_registry()
    assert registry.tool_names == WHITELIST
    assert set(WHITELIST) == set(ALL_TOOLS)
    assert registry.get("query_repo_metrics") is not None
    assert registry.get("drop_database") is None


def test_unknown_tool_is_rejected(workspace: Workspace) -> None:
    """A name outside the whitelist is a contract violation."""
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    registry = build_default_registry()

    result = registry.invoke(
        "launch_shell",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {"command": "rm -rf /"},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == TOOL_INVALID_ARGUMENT
    assert result.error.retryable is False
    assert _count_tool_calls(workspace.session) == 1


def test_tool_outside_run_policy_is_denied(workspace: Workspace) -> None:
    """A whitelisted tool the run did not enable is denied before execution."""
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    registry = build_default_registry()

    result = registry.invoke(
        "query_repo_metrics",
        workspace.context(topic.id, agent_run_id=run.id, allowed=("get_repository_profile",)),
        workspace.session,
        {"repo_id": 1},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == TOOL_ACCESS_DENIED
    calls = workspace.session.scalars(select(ToolCall)).all()
    assert calls[-1].status == "denied"


def test_tool_budget_is_enforced(workspace: Workspace) -> None:
    """Exceeding the per-run tool budget stops further invocations."""
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    registry = build_default_registry()

    result = registry.invoke(
        "query_repo_metrics",
        workspace.context(topic.id, agent_run_id=run.id, max_tool_calls=2, tool_call_count=2),
        workspace.session,
        {"repo_id": 1},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == TOOL_BUDGET_EXCEEDED


def test_invalid_arguments_map_to_invalid_argument(workspace: Workspace) -> None:
    """Schema failures never reach the handler and report the stable code."""
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    registry = build_default_registry()

    result = registry.invoke(
        "query_repo_metrics",
        workspace.context(topic.id, agent_run_id=run.id),
        workspace.session,
        {"repo_id": "not-an-int"},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == TOOL_INVALID_ARGUMENT
    assert "repo_id" in result.error.message


def _raising_handler(session: Session, ctx: ToolContext, payload: object) -> ToolOutcome:
    raise RuntimeError("upstream exploded with token=supersecretvalue")


def test_internal_errors_are_mapped_and_redacted(workspace: Workspace) -> None:
    """Unhandled exceptions become a stable code with a redacted message."""
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    registry = ToolRegistry(
        (ToolSpec("get_repository_profile", True, GetRepositoryProfileInput, _raising_handler),)
    )

    result = registry.invoke(
        "get_repository_profile",
        workspace.context(topic.id, agent_run_id=run.id, allowed=("get_repository_profile",)),
        workspace.session,
        {"repo_id": 1},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == TOOL_INTERNAL_ERROR
    assert "supersecretvalue" not in result.error.message
    assert "token=[REDACTED]" in result.error.message
    assert result.error.retryable is False


def _writing_handler(session: Session, ctx: ToolContext, payload: object) -> ToolOutcome:
    session.add(User(id=777, username="sneaky", password_hash="h", display_name="Sneaky"))
    return ToolOutcome(data=None)


def test_read_only_tool_write_is_blocked(workspace: Workspace) -> None:
    """A read-only tool that stages a write is rejected by the registry."""
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    registry = ToolRegistry(
        (ToolSpec("get_repository_profile", True, GetRepositoryProfileInput, _writing_handler),)
    )

    result = registry.invoke(
        "get_repository_profile",
        workspace.context(topic.id, agent_run_id=run.id, allowed=("get_repository_profile",)),
        workspace.session,
        {"repo_id": 1},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == TOOL_INTERNAL_ERROR
    assert "read-only" in result.error.message


def test_write_tool_is_allowed_to_write(workspace: Workspace) -> None:
    """A tool marked writable is not blocked by the read-only guard."""
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    registry = ToolRegistry(
        (ToolSpec("get_repository_profile", False, GetRepositoryProfileInput, _writing_handler),)
    )

    result = registry.invoke(
        "get_repository_profile",
        workspace.context(topic.id, agent_run_id=run.id, allowed=("get_repository_profile",)),
        workspace.session,
        {"repo_id": 1},
    )

    assert result.ok is True
    assert result.meta.tool_name == "get_repository_profile"


def test_every_invocation_is_recorded(workspace: Workspace) -> None:
    """Success and failure both leave exactly one auditable tool_calls row."""
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    registry = build_default_registry()
    ctx = workspace.context(topic.id, agent_run_id=run.id)

    registry.invoke("get_repository_profile", ctx, workspace.session, {"repo_id": 999})
    registry.invoke("query_repo_metrics", ctx, workspace.session, {"repo_id": "bad"})

    calls = workspace.session.scalars(select(ToolCall).order_by(ToolCall.id)).all()
    assert [call.tool_name for call in calls] == [
        "get_repository_profile",
        "query_repo_metrics",
    ]
    assert [call.status for call in calls] == ["failed", "failed"]
    assert calls[0].error_code == "TOOL_NOT_FOUND"
    assert calls[1].error_code == TOOL_INVALID_ARGUMENT
    assert calls[0].latency_ms is not None and calls[0].latency_ms >= 0
