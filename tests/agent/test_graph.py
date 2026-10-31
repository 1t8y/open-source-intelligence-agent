"""Tests for the bounded research graph wiring and its routing decisions."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest

from open_source_intelligence.agent.graph import GRAPH_NODES, _route_after_verify, _verify_node
from open_source_intelligence.agent.llm.messages import ModelToolCall
from open_source_intelligence.agent.state import AgentState, EvidenceRef, VerificationResult


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


def _evidence(*, tool_name: str, repo_id: int | None = None, ok: bool = True) -> EvidenceRef:
    return EvidenceRef(
        tool_name=tool_name,
        repo_id=repo_id,
        ok=ok,
        result_count=1,
        truncated=False,
    )


def _verify_deps() -> Mock:
    """A dependency double exposing the two things `_verify_node` reads."""
    deps = Mock()
    deps.run_verifier = True
    deps.recorder.record.return_value = Mock()
    return deps


def test_graph_node_order_is_fixed() -> None:
    assert GRAPH_NODES == ("plan", "tool", "observe", "verify", "write")


def test_verify_nudges_the_planner_when_no_content_was_read() -> None:
    """A missing README read is the one gap the model keeps leaving open; say so once.

    Without the nudge the planner keeps doing profile + metrics + history and never reads cached
    content, so the verifier stays "insufficient" until the budget runs out. The nudge must fire
    exactly once and name the missing tool.
    """
    state = _state(
        candidate_repo_ids=[1, 2],
        evidence=[
            _evidence(tool_name="get_repository_profile", repo_id=1),
            _evidence(tool_name="query_repo_metrics", repo_id=1),
            _evidence(tool_name="search_prior_reports"),
        ],
    )

    result = _verify_node(state, _verify_deps())  # type: ignore[arg-type]

    assert result["verification"].sufficient is False
    assert len(result["messages"]) == 1
    assert "get_repository_content" in result["messages"][0].content
    assert result["readme_nudged"] is True


def test_verify_does_not_nudge_twice() -> None:
    """The nudge is a one-shot: a run that still cannot read content must not pile it up."""
    state = _state(
        candidate_repo_ids=[1],
        evidence=[_evidence(tool_name="get_repository_profile", repo_id=1)],
        readme_nudged=True,
    )

    result = _verify_node(state, _verify_deps())  # type: ignore[arg-type]

    assert result["messages"] == []


def test_verify_does_not_nudge_when_content_was_read() -> None:
    """Once a README is in the evidence, there is nothing to nudge about."""
    state = _state(
        candidate_repo_ids=[1],
        evidence=[
            _evidence(tool_name="get_repository_profile", repo_id=1),
            _evidence(tool_name="query_repo_metrics", repo_id=1),
            _evidence(tool_name="search_prior_reports"),
            _evidence(tool_name="get_repository_content", repo_id=1),
        ],
    )

    result = _verify_node(state, _verify_deps())  # type: ignore[arg-type]

    assert result["verification"].sufficient is True
    assert result["messages"] == []


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
