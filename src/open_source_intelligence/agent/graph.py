"""Bounded LangGraph research graph for one topic window.

Flow (ARCHITECTURE.md §8.1)::

    Research -> Tool Call -> Observe -> Verify -> Write
                        ^                  |
                        +---- evidence gap -+

The graph is deliberately bounded. Loop, model-call, and tool-call caps all come from
`agent_configs`; when a cap is hit the graph stops researching and writes whatever the
evidence supports, marking the draft incomplete rather than looping forever.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel
from sqlalchemy.orm import Session

from open_source_intelligence.agent.errors import (
    AGENT_BUDGET_EXCEEDED,
    AGENT_DRAFT_INVALID,
    AGENT_EVIDENCE_INCOMPLETE,
    AGENT_MODEL_UNAVAILABLE,
    AGENT_NO_CANDIDATES,
    AGENT_PROMPT_INJECTION,
    AGENT_WRITE_FAILED,
    REVIEW_REQUIRED_CODES,
)
from open_source_intelligence.agent.guards import GuardDecision, guard_tool_payload
from open_source_intelligence.agent.llm.base import ModelClient
from open_source_intelligence.agent.llm.errors import ModelClientError
from open_source_intelligence.agent.llm.messages import (
    ChatMessage,
    ToolSchema,
    tool_error_payload,
)
from open_source_intelligence.agent.profile import WRITE_TOOL
from open_source_intelligence.agent.recording import StepRecorder, digest_text
from open_source_intelligence.agent.state import (
    AgentState,
    EvidenceRef,
    ResearchIntent,
    ResearchTask,
    ToolObservation,
    VerificationResult,
)
from open_source_intelligence.agent.tools.context import ToolContext
from open_source_intelligence.agent.tools.contracts import (
    REPORT_SECTION_KEYS,
    SectionRef,
    ToolResult,
)
from open_source_intelligence.agent.tools.registry import ToolRegistry
from open_source_intelligence.agent.writing import (
    DraftPayloadError,
    DraftSection,
    parse_draft_payload,
)
from open_source_intelligence.core.text import content_digest

#: The draft-writing tool. `profile.WRITE_TOOL` is the single definition; this alias keeps the
#: name the graph has always exported for tests and documentation.
SAVE_SECTION_TOOL = WRITE_TOOL
TOKEN_DEGRADE_PERCENT = 80
#: How many times the write stage may ask the model for a draft. Two attempts is the point where a
#: schema mistake or a dropped section stops being a coin flip without letting a run that cannot
#: follow the contract spend the whole budget on retries.
DRAFT_ATTEMPTS = 2

_INTENT_BY_TOOL: dict[str, ResearchIntent] = {
    "get_repository_profile": "profile",
    "query_repo_metrics": "metrics",
    "search_prior_reports": "prior_reports",
    "get_repository_content": "content",
}


@dataclass(slots=True)
class GraphDependencies:
    """Everything a graph node needs, injected once per run."""

    client: ModelClient
    registry: ToolRegistry
    session: Session
    recorder: StepRecorder
    context: ToolContext
    draft_prompt: str
    tool_schemas: tuple[ToolSchema, ...] = ()
    run_verifier: bool = True


def _elapsed_ms(started: float) -> int:
    """Return a non-negative elapsed millisecond count."""
    return max(0, int((time.perf_counter() - started) * 1000))


def _draft_messages(
    messages: list[ChatMessage],
    draft_prompt: str,
    rejection: str | None = None,
) -> list[ChatMessage]:
    """Swap the research system prompt for the drafting prompt.

    The plan stage and the write stage are different tasks with different output
    contracts, and `prompts/` versions them separately. Replacing the system message keeps
    the recorded user context and tool trail intact while giving the model the draft
    instructions (section keys, citation format, JSON shape) it is actually being asked for.

    `rejection` carries the reason the previous attempt was not accepted. Retrying without it
    mostly reproduces the same mistake — the model has no way to know which of the six sections
    it dropped or which field failed validation.
    """
    if not draft_prompt:
        return list(messages)
    prepared = [
        ChatMessage(role="system", content=draft_prompt),
        *(message for message in messages if message.role != "system"),
    ]
    if rejection:
        prepared.append(
            ChatMessage(
                role="user",
                content=(
                    f"上一份草稿未被接受，原因是：{rejection}\n"
                    "请重新输出完整的 JSON 对象：sections 是数组，六个 section_key 各出现一次，"
                    "每节都带至少一条 citations；除了这个 JSON 对象不要输出任何其它文字。"
                ),
            )
        )
    return prepared


def _as_repo_id(value: Any) -> int | None:
    """Return a positive repository id, ignoring booleans and other junk."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _jsonable(value: Any) -> Any:
    """Convert tool payloads into JSON-safe structures for the conversation log."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _tool_message_content(result: ToolResult[Any]) -> tuple[str, GuardDecision]:
    """Render one tool result as the JSON the model reads back."""
    if result.ok:
        payload, decision = guard_tool_payload(_jsonable(result.data))
        return json.dumps(payload, ensure_ascii=False), decision
    if result.error is not None:
        return (
            json.dumps(
                tool_error_payload(result.error.code, result.error.message),
                ensure_ascii=False,
            ),
            GuardDecision(False, 0),
        )
    return (
        json.dumps(tool_error_payload("UNKNOWN", "tool failed"), ensure_ascii=False),
        GuardDecision(False, 0),
    )


def _total_tokens(state: AgentState) -> int:
    """Return the current total token count recorded in the graph state."""
    return state.get("prompt_tokens", 0) + state.get("completion_tokens", 0)


def _token_budget_at_or_above(state: AgentState, percent: int) -> bool:
    """Return whether the configured token budget reached the given percentage."""
    max_tokens = state.get("max_tokens", 0)
    return max_tokens > 0 and _total_tokens(state) * 100 >= max_tokens * percent


def _cost_budget_exhausted(state: AgentState) -> bool:
    """Return whether the run has already spent its configured cost budget.

    Cost is a budget dimension of its own: token caps bound how much text flows through the
    model, while a price table can make a modest number of expensive calls cost more than a
    large number of cheap ones. `agent_configs.max_cost` is therefore enforced here, next to
    the token gate, instead of being written to the config and never read again.
    """
    max_cost = state.get("max_cost", Decimal("0"))
    spent = state.get("estimated_cost", Decimal("0"))
    return max_cost > 0 and spent >= max_cost


def _observation_summary(observation: ToolObservation) -> str:
    """Return a compact human-readable line for one observation."""
    if not observation.ok:
        return f"{observation.tool_name} failed with {observation.error_code or 'UNKNOWN'}"
    suffix = " (truncated)" if observation.truncated else ""
    return f"{observation.tool_name} returned {observation.result_count} record(s){suffix}"


def _plan_node(state: AgentState, deps: GraphDependencies) -> dict[str, Any]:
    """Ask the model what to research next and stage its tool calls."""
    started = time.perf_counter()
    loop_count = state.get("loop_count", 0)
    model_call_count = state.get("model_call_count", 0)
    max_model_calls = state.get("max_model_calls", 0)
    prompt_tokens = state.get("prompt_tokens", 0)
    completion_tokens = state.get("completion_tokens", 0)

    if state.get("guard_triggered"):
        deps.recorder.record(
            step_type="plan",
            status="skipped",
            summary="prompt guard triggered; stopping further research calls",
            latency_ms=_elapsed_ms(started),
        )
        return {"pending_tool_calls": [], "loop_count": loop_count + 1}

    if max_model_calls > 0 and model_call_count >= max_model_calls:
        deps.recorder.record(
            step_type="plan",
            status="skipped",
            summary="model call budget exhausted; stopping research",
            latency_ms=_elapsed_ms(started),
        )
        return {
            "pending_tool_calls": [],
            "loop_count": loop_count + 1,
            "budget_degraded": True,
        }

    if _token_budget_at_or_above(state, 100):
        deps.recorder.record(
            step_type="plan",
            status="skipped",
            summary="token budget exhausted; stopping research",
            latency_ms=_elapsed_ms(started),
        )
        return {
            "pending_tool_calls": [],
            "loop_count": loop_count + 1,
            "budget_degraded": True,
        }

    if _token_budget_at_or_above(state, TOKEN_DEGRADE_PERCENT):
        deps.recorder.record(
            step_type="plan",
            status="degraded",
            summary="token budget reached 80%; finishing evidence collection",
            latency_ms=_elapsed_ms(started),
        )
        return {
            "pending_tool_calls": [],
            "loop_count": loop_count + 1,
            "budget_degraded": True,
        }

    if _cost_budget_exhausted(state):
        deps.recorder.record(
            step_type="plan",
            status="skipped",
            summary="cost budget exhausted; stopping research",
            latency_ms=_elapsed_ms(started),
        )
        return {
            "pending_tool_calls": [],
            "loop_count": loop_count + 1,
            "budget_degraded": True,
        }

    messages = list(state.get("messages") or [])
    try:
        turn = deps.client.complete(messages, deps.tool_schemas)
    except ModelClientError as exc:
        deps.recorder.record(
            step_type="plan",
            status="failed",
            summary=f"model call failed with {exc.code}",
            latency_ms=_elapsed_ms(started),
        )
        # A failed call can still have been billed (an answer truncated by the output ceiling
        # arrives, is unusable, and is charged for). Those tokens and that cost are added here
        # so the budget moves forward instead of letting one run retry for free.
        return {
            "pending_tool_calls": [],
            "loop_count": loop_count + 1,
            "model_call_count": model_call_count + 1,
            "prompt_tokens": prompt_tokens + exc.prompt_tokens,
            "completion_tokens": completion_tokens + exc.completion_tokens,
            "estimated_cost": state.get("estimated_cost", Decimal("0")) + exc.cost,
            "status": "failed",
            "error_code": AGENT_MODEL_UNAVAILABLE,
            "error_message": exc.message,
        }

    plan = [
        ResearchTask(
            task_index=index,
            intent=_INTENT_BY_TOOL.get(call.name, "profile"),
            repo_id=_as_repo_id(call.arguments.get("repo_id")),
        )
        for index, call in enumerate(turn.tool_calls, start=1)
    ]
    deps.recorder.record(
        step_type="plan",
        status="ok",
        summary=f"planned {len(turn.tool_calls)} tool call(s) on loop {loop_count + 1}",
        output_digest=digest_text(json.dumps(_jsonable(list(turn.tool_calls)), ensure_ascii=False)),
        latency_ms=_elapsed_ms(started),
    )
    next_prompt_tokens = prompt_tokens + turn.prompt_tokens
    next_completion_tokens = completion_tokens + turn.completion_tokens
    next_state: dict[str, Any] = {
        "messages": [
            ChatMessage(role="assistant", content=turn.text, tool_calls=turn.tool_calls),
        ],
        "pending_tool_calls": list(turn.tool_calls),
        "research_plan": plan,
        "loop_count": loop_count + 1,
        "model_call_count": model_call_count + 1,
        "prompt_tokens": next_prompt_tokens,
        "completion_tokens": next_completion_tokens,
        "estimated_cost": state.get("estimated_cost", Decimal("0")) + turn.cost,
    }
    token_total = next_prompt_tokens + next_completion_tokens
    max_tokens = state.get("max_tokens", 0)
    if max_tokens > 0 and token_total * 100 >= max_tokens * TOKEN_DEGRADE_PERCENT:
        next_state["budget_degraded"] = True
    return next_state


def _tool_node(state: AgentState, deps: GraphDependencies) -> dict[str, Any]:
    """Execute every staged tool call through the whitelist registry."""
    started = time.perf_counter()
    calls = list(state.get("pending_tool_calls") or [])
    tool_call_count = state.get("tool_call_count", 0)
    guard_triggered = bool(state.get("guard_triggered"))
    messages: list[ChatMessage] = []
    observations: list[ToolObservation] = []
    # Calls are committed one by one as they run (evidence has to survive a crash); the step is
    # written afterwards, so remember where this node started and attach that tail to it.
    first_call = len(deps.context.recorded_call_ids)

    for call in calls:
        context = replace(deps.context, tool_call_count=tool_call_count)
        result = deps.registry.invoke(call.name, context, deps.session, call.arguments)
        tool_call_count += 1
        content, guard_decision = _tool_message_content(result)
        guard_triggered = guard_triggered or guard_decision.triggered
        messages.append(
            ChatMessage(
                role="tool",
                name=call.name,
                tool_call_id=call.call_id,
                content=content,
            )
        )
        observations.append(
            ToolObservation(
                tool_name=call.name,
                repo_id=_as_repo_id(call.arguments.get("repo_id")),
                ok=result.ok,
                result_count=result.meta.result_count,
                truncated=result.meta.truncated,
                error_code=None if result.error is None else result.error.code,
                payload=result.data,
                guard_triggered=guard_decision.triggered,
            )
        )

    status = "ok" if all(item.ok for item in observations) else "partial"
    step = deps.recorder.record(
        step_type="tool",
        status=status,
        summary=f"executed {len(observations)} tool call(s)",
        latency_ms=_elapsed_ms(started),
    )
    deps.recorder.link_tool_calls(step.id, deps.context.recorded_call_ids[first_call:])
    return {
        "messages": messages,
        "last_observations": observations,
        "tool_call_count": tool_call_count,
        "guard_triggered": guard_triggered,
    }


def _observe_node(state: AgentState, deps: GraphDependencies) -> dict[str, Any]:
    """Summarize the latest tool outcomes into audit-friendly evidence rows."""
    started = time.perf_counter()
    observations = list(state.get("last_observations") or [])
    guard_triggered = bool(state.get("guard_triggered"))
    evidence = [
        EvidenceRef(
            tool_name=observation.tool_name,
            repo_id=observation.repo_id,
            ok=observation.ok,
            result_count=observation.result_count,
            truncated=observation.truncated,
            error_code=observation.error_code,
            summary=_observation_summary(observation),
            guard_triggered=observation.guard_triggered,
        )
        for observation in observations
    ]
    guard_triggered = guard_triggered or any(item.guard_triggered for item in evidence)
    deps.recorder.record(
        step_type="observe",
        status="ok",
        summary=f"absorbed {len(evidence)} observation(s)",
        latency_ms=_elapsed_ms(started),
    )
    return {"evidence": evidence, "guard_triggered": guard_triggered}


def _verify_node(state: AgentState, deps: GraphDependencies) -> dict[str, Any]:
    """Check whether the evidence base can carry an internal weekly draft.

    A draft needs more than counters: every candidate must carry a profile and a metric
    series, the topic's published history must have been consulted for a baseline, and at
    least one candidate must have had its cached content read.
    """
    started = time.perf_counter()
    if not deps.run_verifier:
        # `agent_no_verifier` ablation (`docs/evaluation.md` §8): the coverage gate is removed
        # so a run that would normally be held back for a human instead publishes whatever the
        # evidence supports. The result is recorded as a normal verification so the graph and
        # the run summary stay identical in shape to a verified run - the whole point of the
        # ablation is to measure the gate's contribution, not to change the pipeline's contract.
        permissive = VerificationResult(
            sufficient=True,
            reason="verifier disabled by execution profile",
            covered_repo_ids=[],
            missing_repo_ids=[],
            content_repo_ids=[],
            has_prior_baseline=False,
            security_risk=False,
        )
        deps.recorder.record(
            step_type="verify",
            status="skipped",
            summary=permissive.reason,
            latency_ms=_elapsed_ms(started),
        )
        return {"verification": permissive}

    candidates = list(state.get("candidate_repo_ids") or [])
    evidence = list(state.get("evidence") or [])
    guard_triggered = bool(state.get("guard_triggered")) or any(
        item.guard_triggered for item in evidence
    )
    profiles = {
        item.repo_id
        for item in evidence
        if item.ok and item.tool_name == "get_repository_profile" and item.repo_id is not None
    }
    metrics = {
        item.repo_id
        for item in evidence
        if item.ok and item.tool_name == "query_repo_metrics" and item.repo_id is not None
    }
    content_repos = {
        item.repo_id
        for item in evidence
        if item.ok and item.tool_name == "get_repository_content" and item.repo_id is not None
    }
    has_prior_baseline = any(
        item.ok and item.tool_name == "search_prior_reports" for item in evidence
    )
    covered = sorted(
        repo_id for repo_id in candidates if repo_id in profiles and repo_id in metrics
    )
    missing = sorted(
        repo_id for repo_id in candidates if repo_id not in profiles or repo_id not in metrics
    )

    reasons: list[str] = []
    if not candidates:
        reasons.append("no candidate repository was selected for this topic")
    if missing:
        reasons.append(f"{len(missing)} candidate repository(ies) lack profile or metric evidence")
    if candidates and not has_prior_baseline:
        reasons.append("no prior report baseline was queried")
    if candidates and not content_repos:
        reasons.append("no cached repository content was read")
    if guard_triggered:
        reasons.append("untrusted content contained instruction-like text")

    result = VerificationResult(
        sufficient=not reasons,
        reason="; ".join(reasons) if reasons else "evidence base complete for an internal draft",
        covered_repo_ids=covered,
        missing_repo_ids=missing,
        content_repo_ids=sorted(content_repos),
        has_prior_baseline=has_prior_baseline,
        security_risk=guard_triggered,
    )
    deps.recorder.record(
        step_type="verify",
        status="ok" if result.sufficient else "insufficient",
        summary=result.reason,
        latency_ms=_elapsed_ms(started),
    )

    # A nudge for the one gap the model keeps leaving open: it does profile + metrics + history
    # and then stops, never reading a README, so the verifier stays "insufficient" until the
    # budget runs out. When the only missing class is cached content, tell the planner exactly
    # what is missing instead of letting it guess. Sent once, so a run that still cannot satisfy
    # the condition does not accumulate the same instruction every loop.
    nudge_messages: list[ChatMessage] = []
    if (
        not result.sufficient
        and candidates
        and not content_repos
        and not guard_triggered
        and not state.get("readme_nudged", False)
    ):
        nudge_messages = [
            ChatMessage(
                role="user",
                content=(
                    "证据校验仍未通过：你已经为所有候选仓库取了画像和指标、查询了历史报告，"
                    "但还没有阅读任何一份候选仓库的缓存内容。请调用 `get_repository_content` "
                    "至少阅读一个候选仓库（优先排名最靠前的），否则整轮证据会被判为不完整。"
                ),
            )
        ]
    return {
        "verification": result,
        "guard_triggered": guard_triggered,
        "readme_nudged": bool(state.get("readme_nudged", False)) or bool(nudge_messages),
        "messages": nudge_messages,
    }


def _write_node(state: AgentState, deps: GraphDependencies) -> dict[str, Any]:
    """Draft the report sections and persist them through `save_report_section`."""
    started = time.perf_counter()
    verification = state.get("verification")
    sufficient = verification is not None and verification.sufficient
    model_call_count = state.get("model_call_count", 0)
    max_model_calls = state.get("max_model_calls", 0)
    prompt_tokens = state.get("prompt_tokens", 0)
    completion_tokens = state.get("completion_tokens", 0)
    max_tokens = state.get("max_tokens", 0)
    budget_degraded = bool(state.get("budget_degraded"))
    guard_triggered = bool(state.get("guard_triggered"))

    # A fatal upstream failure (the provider was unreachable during planning, a call was
    # rejected) must end the run as that failure. Without this gate the graph flows plan -> write
    # unconditionally, the write stage returns `error_code=None` for a clean draft, and the
    # terminal status is decided from that last value: the run would be recorded as
    # "needs_review" and the retry rule keyed on AGENT_MODEL_UNAVAILABLE would never fire, while
    # a second call went out to an endpoint that had just failed.
    carried_error = state.get("error_code")
    if carried_error is not None and carried_error not in REVIEW_REQUIRED_CODES:
        deps.recorder.record(
            step_type="write",
            status="skipped",
            summary=f"upstream failure {carried_error}; draft writing skipped",
            latency_ms=_elapsed_ms(started),
        )
        return {
            "incomplete": True,
            "status": "failed",
            "error_code": carried_error,
            "error_message": state.get("error_message"),
        }

    if not state.get("candidate_repo_ids"):
        deps.recorder.record(
            step_type="write",
            status="skipped",
            summary="no candidate repository was selected; draft writing skipped",
            latency_ms=_elapsed_ms(started),
        )
        return {
            "incomplete": True,
            "status": "needs_review",
            "error_code": AGENT_NO_CANDIDATES,
            "error_message": "no candidate repository was selected for this topic",
        }

    if SAVE_SECTION_TOOL not in deps.context.allowed_tools:
        deps.recorder.record(
            step_type="write",
            status="skipped",
            summary="draft writing is not enabled by the tool policy",
            latency_ms=_elapsed_ms(started),
        )
        return {
            "incomplete": True,
            "status": "needs_review",
            "error_code": AGENT_WRITE_FAILED,
            "error_message": "draft writing is not enabled by the tool policy",
        }

    if max_model_calls > 0 and model_call_count >= max_model_calls:
        deps.recorder.record(
            step_type="write",
            status="skipped",
            summary="model call budget exhausted before drafting",
            latency_ms=_elapsed_ms(started),
        )
        return {
            "incomplete": True,
            "status": "needs_review",
            "error_code": AGENT_BUDGET_EXCEEDED,
            "error_message": "model call budget exhausted before drafting",
        }

    if max_tokens > 0 and prompt_tokens + completion_tokens >= max_tokens:
        deps.recorder.record(
            step_type="write",
            status="skipped",
            summary="token budget exhausted before drafting",
            latency_ms=_elapsed_ms(started),
        )
        return {
            "incomplete": True,
            "status": "needs_review",
            "error_code": AGENT_BUDGET_EXCEEDED,
            "error_message": "token budget exhausted before drafting",
            "budget_degraded": True,
        }

    if _cost_budget_exhausted(state):
        deps.recorder.record(
            step_type="write",
            status="skipped",
            summary="cost budget exhausted before drafting",
            latency_ms=_elapsed_ms(started),
        )
        return {
            "incomplete": True,
            "status": "needs_review",
            "error_code": AGENT_BUDGET_EXCEEDED,
            "error_message": "cost budget exhausted before drafting",
            "budget_degraded": True,
        }

    messages = list(state.get("messages") or [])
    # One draft call is a coin flip on a real model: the answer can fail the schema, or come back
    # with one of the six sections. Both are worth asking again about, because the second attempt
    # is told exactly what was wrong — but neither is worth asking about forever, so the loop is
    # capped and every attempt is charged against the same model-call, token, and cost budgets.
    sections: list[DraftSection] = []
    rejection: str | None = None
    cost = state.get("estimated_cost", Decimal("0"))
    max_cost = state.get("max_cost", Decimal("0"))
    attempts = 0
    turn = None
    while attempts < DRAFT_ATTEMPTS:
        if max_model_calls > 0 and model_call_count + attempts >= max_model_calls:
            break
        if max_tokens > 0 and prompt_tokens + completion_tokens >= max_tokens:
            break
        if max_cost > 0 and cost >= max_cost:
            break
        attempts += 1
        try:
            turn = deps.client.complete(_draft_messages(messages, deps.draft_prompt, rejection), ())
        except ModelClientError as exc:
            deps.recorder.record(
                step_type="write",
                status="failed",
                summary=f"draft model call failed with {exc.code}",
                latency_ms=_elapsed_ms(started),
            )
            return {
                "incomplete": True,
                "status": "failed",
                "error_code": AGENT_MODEL_UNAVAILABLE,
                "error_message": exc.message,
                "model_call_count": model_call_count + attempts,
                "prompt_tokens": prompt_tokens + exc.prompt_tokens,
                "completion_tokens": completion_tokens + exc.completion_tokens,
                "estimated_cost": cost + exc.cost,
                "budget_degraded": budget_degraded,
                "guard_triggered": guard_triggered,
            }
        prompt_tokens += turn.prompt_tokens
        completion_tokens += turn.completion_tokens
        cost += turn.cost
        try:
            sections = parse_draft_payload(turn.text)
        except DraftPayloadError as exc:
            rejection = str(exc)
            continue
        absent = [
            key for key in REPORT_SECTION_KEYS if key not in {s.section_key for s in sections}
        ]
        if absent:
            rejection = f"缺少章节 {', '.join(absent)}"
            continue
        # A draft that has all six sections but none of its citations is not complete: the
        # generate stage refuses it (`section X has no citations`), so accepting it here would
        # let the run reach `completed` and then fail one step later — after the model call was
        # already paid for. Re-ask, naming the empty sections, the same way a missing section is
        # named. This is the code-side enforcement of the prompt rule "every section carries at
        # least one citation".
        citationless = [section.section_key for section in sections if not section.citations]
        if citationless:
            rejection = f"以下章节没有引用：{', '.join(citationless)}"
            continue
        rejection = None
        break

    if turn is None or (rejection is not None and not sections):
        message = rejection or "no draft attempt could be made within the remaining budget"
        deps.recorder.record(
            step_type="write",
            status="failed",
            summary=f"draft payload rejected: {message}",
            latency_ms=_elapsed_ms(started),
        )
        return {
            "incomplete": True,
            "status": "failed",
            "error_code": AGENT_DRAFT_INVALID,
            "error_message": message,
            "model_call_count": model_call_count + attempts,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "estimated_cost": cost,
            "budget_degraded": budget_degraded,
            "guard_triggered": guard_triggered,
        }

    # A Markdown fallback (the model ignored the JSON contract) can come back with fewer than
    # the six template sections. That is recoverable content, not a crash: save what arrived and
    # flag the run for review, instead of treating a partially-drafted report as a clean
    # completion.
    present_keys = {section.section_key for section in sections}
    missing_keys = [key for key in REPORT_SECTION_KEYS if key not in present_keys]
    draft_incomplete = bool(missing_keys)

    # Chapter writes are deterministic and bounded by the report template, so they run
    # against their own budget and never compete with the research-call cap. That keeps
    # `max_tool_calls` meaning "how much the model may explore", not "how big a report is".
    write_context = replace(
        deps.context,
        tool_call_count=0,
        max_tool_calls=max(1, len(sections)),
    )
    saved: list[SectionRef] = []
    tool_call_count = state.get("tool_call_count", 0)
    failures = 0
    # Same bookkeeping as the tool node: section writes are also `tool_calls` rows, and they belong
    # to the `write` step written below. `replace` copies the list by reference, so the registry
    # appends to the same list the run context holds.
    first_write_call = len(deps.context.recorded_call_ids)
    for index, section in enumerate(sections):
        arguments: dict[str, Any] = {
            "section_key": section.section_key,
            "content_markdown": section.content_markdown,
            "citations": [citation.model_dump() for citation in section.citations],
            "content_digest": content_digest(section.content_markdown),
        }
        context = replace(write_context, tool_call_count=index)
        result = deps.registry.invoke(SAVE_SECTION_TOOL, context, deps.session, arguments)
        tool_call_count += 1
        if result.ok and isinstance(result.data, SectionRef):
            saved.append(result.data)
        else:
            failures += 1

    write_step = deps.recorder.record(
        step_type="write",
        status="ok" if failures == 0 else "partial",
        summary=f"saved {len(saved)} section(s), {failures} failed",
        input_digest=digest_text(turn.text),
        latency_ms=_elapsed_ms(started),
    )
    deps.recorder.link_tool_calls(write_step.id, deps.context.recorded_call_ids[first_write_call:])
    # A partially stored draft is not a finished draft: report it as needing a human instead
    # of letting the run look complete with fewer sections than the template requires.
    next_prompt_tokens = prompt_tokens
    next_completion_tokens = completion_tokens
    next_cost = cost
    token_total = next_prompt_tokens + next_completion_tokens
    max_cost = state.get("max_cost", Decimal("0"))
    # The draft call is billed like any other, so its cost has to be added here as well: a run
    # that planned cheaply and then wrote an expensive draft used to be recorded with only the
    # planning cost.
    budget_exceeded = (max_tokens > 0 and token_total >= max_tokens) or (
        max_cost > 0 and next_cost >= max_cost
    )
    budget_limited = budget_degraded and not sufficient
    if failures:
        write_error_code: str | None = AGENT_WRITE_FAILED
        write_error_message: str | None = f"{failures} section(s) could not be saved"
    elif draft_incomplete:
        write_error_code = AGENT_EVIDENCE_INCOMPLETE
        write_error_message = (
            f"{len(missing_keys)} report section(s) missing: {', '.join(missing_keys)}"
        )
    elif guard_triggered:
        write_error_code = AGENT_PROMPT_INJECTION
        write_error_message = "untrusted content contained instruction-like text"
    elif budget_exceeded or budget_limited:
        write_error_code = AGENT_BUDGET_EXCEEDED
        write_error_message = "budget stopped the research stage"
    else:
        write_error_code = None
        write_error_message = None
    return {
        "messages": [ChatMessage(role="assistant", content=turn.text)],
        "draft_sections": saved,
        "tool_call_count": tool_call_count,
        "model_call_count": model_call_count + attempts,
        "prompt_tokens": next_prompt_tokens,
        "completion_tokens": next_completion_tokens,
        "estimated_cost": next_cost,
        "budget_degraded": budget_degraded or budget_exceeded,
        "guard_triggered": guard_triggered,
        "incomplete": bool(failures)
        or draft_incomplete
        or not sufficient
        or guard_triggered
        or budget_exceeded,
        "status": (
            "needs_review"
            if (
                failures or draft_incomplete or not sufficient or guard_triggered or budget_exceeded
            )
            else "completed"
        ),
        "error_code": write_error_code,
        "error_message": write_error_message,
    }


def _route_after_verify(state: AgentState) -> Literal["plan", "write"]:
    """Continue researching only while there is an unexhausted budget and a next step."""
    verification = state.get("verification")
    if state.get("guard_triggered"):
        return "write"
    if verification is not None and verification.sufficient:
        return "write"
    if state.get("budget_degraded"):
        return "write"
    if not state.get("pending_tool_calls"):
        return "write"
    if state.get("loop_count", 0) >= state.get("max_loops", 0):
        return "write"
    if state.get("model_call_count", 0) >= state.get("max_model_calls", 0):
        return "write"
    if state.get("tool_call_count", 0) >= state.get("max_tool_calls", 0):
        return "write"
    if _token_budget_at_or_above(state, 100):
        return "write"
    if _cost_budget_exhausted(state):
        return "write"
    return "plan"


def build_research_graph(
    deps: GraphDependencies,
) -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    """Compile the bounded research graph with dependencies bound to its nodes."""

    def plan_node(state: AgentState) -> dict[str, Any]:
        return _plan_node(state, deps)

    def tool_node(state: AgentState) -> dict[str, Any]:
        return _tool_node(state, deps)

    def observe_node(state: AgentState) -> dict[str, Any]:
        return _observe_node(state, deps)

    def verify_node(state: AgentState) -> dict[str, Any]:
        return _verify_node(state, deps)

    def write_node(state: AgentState) -> dict[str, Any]:
        return _write_node(state, deps)

    graph: StateGraph[AgentState, None, AgentState, AgentState] = StateGraph(AgentState)
    graph.add_node("plan", plan_node)
    graph.add_node("tool", tool_node)
    graph.add_node("observe", observe_node)
    graph.add_node("verify", verify_node)
    graph.add_node("write", write_node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "tool")
    graph.add_edge("tool", "observe")
    graph.add_edge("observe", "verify")
    graph.add_conditional_edges("verify", _route_after_verify, {"plan": "plan", "write": "write"})
    graph.add_edge("write", END)
    return graph.compile()


#: Node names in execution order, used by tests and documentation.
GRAPH_NODES: tuple[str, ...] = ("plan", "tool", "observe", "verify", "write")
