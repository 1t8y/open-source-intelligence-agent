"""守护有界研究图的接线与其路由决策的用例。"""

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
    """一个依赖替身，只暴露 `_verify_node` 会读的两样东西。"""
    deps = Mock()
    deps.run_verifier = True
    deps.recorder.record.return_value = Mock()
    return deps


def test_graph_node_order_is_fixed() -> None:
    assert GRAPH_NODES == ("plan", "tool", "observe", "verify", "write")


def test_verify_nudges_the_planner_when_no_content_was_read() -> None:
    """证明「没读 README」是模型总留着的那一个缺口，得提醒它一次。

    没有这句提醒，规划器会一直做画像 + 指标 + 历史，从不读缓存正文，于是校验器直到
    预算耗尽都停在「证据不足」。提醒必须只触发一次，并且点出缺的那个工具。
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
    """证明提醒只有一次：仍然读不到正文的运行也不能把它堆起来。"""
    state = _state(
        candidate_repo_ids=[1],
        evidence=[_evidence(tool_name="get_repository_profile", repo_id=1)],
        readme_nudged=True,
    )

    result = _verify_node(state, _verify_deps())  # type: ignore[arg-type]

    assert result["messages"] == []


def test_verify_does_not_nudge_when_content_was_read() -> None:
    """证明一旦证据里有了 README，就没有什么可提醒的了。"""
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
    """证明花到 80% 时，图保留已有证据，但不再继续规划。"""
    state = _state(
        verification=_verification(sufficient=False),
        pending_tool_calls=[_call()],
        prompt_tokens=80,
        max_tokens=100,
        budget_degraded=True,
    )
    assert _route_after_verify(state) == "write"


def test_stops_when_the_token_budget_is_exhausted() -> None:
    """证明 Token 预算彻底耗尽后，不得再触发一次模型调用。"""
    state = _state(
        verification=_verification(sufficient=False),
        pending_tool_calls=[_call()],
        prompt_tokens=100,
        max_tokens=100,
    )
    assert _route_after_verify(state) == "write"


def test_routes_to_write_when_prompt_guard_is_triggered() -> None:
    """证明即使证据仍不完整，不可信指令也会拦下研究工作。"""
    state = _state(
        verification=_verification(sufficient=False),
        pending_tool_calls=[_call()],
        guard_triggered=True,
    )
    assert _route_after_verify(state) == "write"
