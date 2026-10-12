"""Tests for the bounded research graph wiring and its routing decisions."""

from __future__ import annotations

from typing import Any

import pytest

from open_source_intelligence.agent.graph import GRAPH_NODES, _route_after_verify
from open_source_intelligence.agent.llm.messages import ModelToolCall
from open_source_intelligence.agent.state import AgentState, VerificationResult


def _call() -> ModelToolCall:
    return ModelToolCall(call_id="c", name="get_repository_profile", arguments={"repo_id": 1})


def _verification(*, sufficient: bool) -> VerificationResult:
    return VerificationResult(
        sufficient=sufficient,
        reason="test",
        covered_repo_ids=[1] if sufficient else [],
        missing_repo_ids=[] if sufficient else [1],
    )


def _state(**overrides: Any) -> AgentState:
    state: AgentState = {
        "pending_tool_calls": [],
        "loop_count": 0,
        "max_loops": 4,
        "model_call_count": 0,
        "max_model_calls": 6,
        "tool_call_count": 0,
        "max_tool_calls": 10,
        "max_tokens": 1000,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "budget_degraded": False,
        "guard_triggered": False,
        "verification": None,
    }
    state.update(overrides)
    return state


def test_graph_node_order_is_fixed() -> None:
    assert GRAPH_NODES == ("plan", "tool", "observe", "verify", "write")


def test_routes_to_write_once_evidence_is_sufficient() -> None:
    state = _state(verification=_verification(sufficient=True), pending_tool_calls=[_call()])
    assert _route_after_verify(state) == "write"


def test_routes_back_to_plan_while_budget_remains() -> None:
    state = _state(verification=_verification(sufficient=False), pending_tool_calls=[_call()])
    assert _route_after_verify(state) == "plan"


def test_stops_when_the_planner_has_no_next_step() -> None:
    state = _state(verification=_verification(sufficient=False), pending_tool_calls=[])
    assert _route_after_verify(state) == "write"


@pytest.mark.parametrize(
    "overrides",
    [
        {"loop_count": 4, "max_loops": 4},
        {"model_call_count": 6, "max_model_calls": 6},
        {"tool_call_count": 10, "max_tool_calls": 10},
    ],
)
def test_stops_when_any_budget_is_exhausted(overrides: dict[str, int]) -> None:
    state = _state(
        verification=_verification(sufficient=False),
        pending_tool_calls=[_call()],
        **overrides,
    )
    assert _route_after_verify(state) == "write"


def test_stops_without_a_verification_result() -> None:
    state = _state(verification=None, pending_tool_calls=[])
    assert _route_after_verify(state) == "write"


def test_routes_to_write_when_the_token_budget_is_degraded() -> None:
    """At 80% the graph keeps existing evidence but stops further planning."""
    state = _state(
        verification=_verification(sufficient=False),
        pending_tool_calls=[_call()],
        prompt_tokens=80,
        max_tokens=100,
        budget_degraded=True,
    )
    assert _route_after_verify(state) == "write"


def test_stops_when_the_token_budget_is_exhausted() -> None:
    """A fully exhausted Token budget may not trigger another model call."""
    state = _state(
        verification=_verification(sufficient=False),
        pending_tool_calls=[_call()],
        prompt_tokens=100,
        max_tokens=100,
    )
    assert _route_after_verify(state) == "write"


def test_routes_to_write_when_prompt_guard_is_triggered() -> None:
    """Untrusted instructions stop research even if evidence remains incomplete."""
    state = _state(
        verification=_verification(sufficient=False),
        pending_tool_calls=[_call()],
        guard_triggered=True,
    )
    assert _route_after_verify(state) == "write"
