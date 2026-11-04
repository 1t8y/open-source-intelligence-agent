"""单个主题时间窗的有界 LangGraph 研究图。

执行流程（见 `ARCHITECTURE.md` §8.1）::

    Research -> Tool Call -> Observe -> Verify -> Write
                        ^                  |
                        +---- evidence gap -+

这张图刻意有界。轮次上限、模型调用上限与工具调用上限都来自
`agent_configs`；撞上任一上限时，图就停止研究，并把证据能支撑的内容写出来，
同时把草稿标记为不完整，而不是无限循环下去。
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

#: 起草报告的工具。`profile.WRITE_TOOL` 是唯一定义；这个别名保留了图一直
#: 对外导出、供测试与文档使用的那个名字。
SAVE_SECTION_TOOL = WRITE_TOOL
TOKEN_DEGRADE_PERCENT = 80
#: 写阶段最多能向模型要几次草稿。两次是这样一个点：schema 写错或漏掉章节不再像抛
#: 硬币，又不至于让一个跟不上契约的运行把整个预算都花在重试上。
DRAFT_ATTEMPTS = 2

_INTENT_BY_TOOL: dict[str, ResearchIntent] = {
    "get_repository_profile": "profile",
    "query_repo_metrics": "metrics",
    "search_prior_reports": "prior_reports",
    "get_repository_content": "content",
}


@dataclass(slots=True)
class GraphDependencies:
    """图节点所需的一切，每次运行注入一次。"""

    client: ModelClient
    registry: ToolRegistry
    session: Session
    recorder: StepRecorder
    context: ToolContext
    draft_prompt: str
    tool_schemas: tuple[ToolSchema, ...] = ()
    run_verifier: bool = True


def _elapsed_ms(started: float) -> int:
    """返回非负的已耗时毫秒数。"""
    return max(0, int((time.perf_counter() - started) * 1000))


def _draft_messages(
    messages: list[ChatMessage],
    draft_prompt: str,
    rejection: str | None = None,
) -> list[ChatMessage]:
    """把研究的系统提示换成起草提示。

    `plan` 阶段与 `write` 阶段是两个不同的任务，输出契约也不同，`prompts/` 分别
    给它们做版本。替换系统消息可以保留已记录的用户上下文与工具轨迹，同时把模型
    真正被要求的起草指令（章节键名、引用格式、JSON 结构）交给它。

    `rejection` 携带上一次尝试未被接受的原因。不带它重试，多半只是把同一个
    错误再演一遍 —— 模型无从知道它漏掉了六个章节里的哪一个，或者哪个字段没过
    校验。
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
    """返回正的仓库 id，忽略布尔值和其他垃圾值。"""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _jsonable(value: Any) -> Any:
    """把工具载荷转换成可写入对话日志的 JSON 安全结构。"""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _tool_message_content(result: ToolResult[Any]) -> tuple[str, GuardDecision]:
    """把一条工具结果渲染成模型回读的 JSON。"""
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
    """返回图状态里记录的当前 token 总数。"""
    return state.get("prompt_tokens", 0) + state.get("completion_tokens", 0)


def _token_budget_at_or_above(state: AgentState, percent: int) -> bool:
    """判断已用 token 是否达到配置预算的给定百分比。"""
    max_tokens = state.get("max_tokens", 0)
    return max_tokens > 0 and _total_tokens(state) * 100 >= max_tokens * percent


def _cost_budget_exhausted(state: AgentState) -> bool:
    """判断本次运行是否已经花光配置的费用预算。

    费用是独立的预算维度：token 上限约束的是流经模型的文本量，而一份价目表可以让
    少量昂贵调用比大量廉价调用更贵。因此 `agent_configs.max_cost` 在此处、紧挨着
    token 闸执行，而不是写进配置后再也没人读。
    """
    max_cost = state.get("max_cost", Decimal("0"))
    spent = state.get("estimated_cost", Decimal("0"))
    return max_cost > 0 and spent >= max_cost


def _observation_summary(observation: ToolObservation) -> str:
    """为一条观测返回一行紧凑、人类可读的说明。"""
    if not observation.ok:
        return f"{observation.tool_name} failed with {observation.error_code or 'UNKNOWN'}"
    suffix = " (truncated)" if observation.truncated else ""
    return f"{observation.tool_name} returned {observation.result_count} record(s){suffix}"


def _plan_node(state: AgentState, deps: GraphDependencies) -> dict[str, Any]:
    """询问模型下一步研究什么，并暂存它要发起的工具调用。"""
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
        # 失败的调用同样可能已经计费（被输出上限截断的答案照样会返回、不可用，
        # 并且照收费）。把这些 token 与费用加在这里，预算才会往前走，而不是让
        # 一次运行白白重试。
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
    """通过白名单注册表执行每一条已暂存的工具调用。"""
    started = time.perf_counter()
    calls = list(state.get("pending_tool_calls") or [])
    tool_call_count = state.get("tool_call_count", 0)
    guard_triggered = bool(state.get("guard_triggered"))
    messages: list[ChatMessage] = []
    observations: list[ToolObservation] = []
    # 调用执行时逐条提交（证据必须扛过崩溃）；步骤行是之后才写的，所以记下
    # 本节点的起点，再把这批尾巴挂上去。
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
    """把最新的工具结果汇总成便于审计的证据行。"""
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
    """检查这份证据基础能否支撑一份内部周报草稿。

    草稿需要的远不止计数器：每个候选都必须带画像与指标序列，必须查过该主题
    已发布的历史以取得基线，并且至少有一个候选读过它缓存的内容。
    """
    started = time.perf_counter()
    if not deps.run_verifier:
        # `agent_no_verifier` 消融（`docs/evaluation.md` §8）：覆盖闸被移除，
        # 于是平时会被拦下来等人工的运行，改为发布证据能支撑的内容。结果仍按
        # 一次正常校验记录，让图与运行摘要在形状上与已校验的运行完全一致 ——
        # 消融的全部意义是衡量这道闸的贡献，而不是改变流水线的契约。
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

    # 针对模型反复留着不补的那个缺口推一把：它做完画像 + 指标 + 历史就
    # 停下，从不读 README，于是验证器一直「不充分」，直到预算耗尽。当
    # 唯一缺的一类就是缓存内容时，直接告诉规划器缺什么，而不是让它去猜。
    # 只发一次，这样仍然满足不了条件的运行不会每轮都堆积同一句话。
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
    """起草报告各章节，并通过 `save_report_section` 落库。"""
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

    # 上游致命失败（规划期间供应商不可达、某次调用被拒）必须以该失败作为
    # 运行的结局。没有这道闸，图会无条件从 `plan` 流到 `write`，写阶段对
    # 一份干净的草稿返回 `error_code=None`，而终态正是由最后这个值决定
    # 的：运行会被记成 "needs_review"，按 `AGENT_MODEL_UNAVAILABLE`
    # 建立的 retry 规则永远不会触发，同时第二次调用还发向了一个刚刚失败
    # 的端点。
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
    # 在真实模型上，一次草稿调用如同抛硬币：答案可能过不了 schema，也可能
    # 少了一节。两者都值得再问一次，因为第二次尝试会被告知究竟哪里不对 ——
    # 但也都不值得一直问，所以循环有上限，且每次尝试都计入同一份模型调用、
    # token 与费用预算。
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
        # 六个章节齐全却一条引用都没有的草稿不算完整：生成阶段会拒掉它
        # （`section X has no citations`），所以在这里放行会让运行走到
        # `completed`，然后在下一步失败 —— 而那时模型调用已经付过钱了。像点名
        # 缺失章节那样，点名没有引用的章节再要一次。这是对 prompt 规则
        # 「每节至少带一条引用」的代码侧落实。
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

    # Markdown 兜底（模型无视了 JSON 契约）可能只带回少于六个模板章节。
    # 这是可恢复的内容而非崩溃：把到手的存下来并把运行标记为待复核，而不是
    # 把一份只写了一半的报告当成干净完成。
    present_keys = {section.section_key for section in sections}
    missing_keys = [key for key in REPORT_SECTION_KEYS if key not in present_keys]
    draft_incomplete = bool(missing_keys)

    # 章节写入是确定性的，且受报告模板约束，所以它们走自己的预算，从不与
    # 研究调用上限争抢。这样 `max_tool_calls` 的含义才是「模型可以探索
    # 多少」，而不是「报告有多大」。
    write_context = replace(
        deps.context,
        tool_call_count=0,
        max_tool_calls=max(1, len(sections)),
    )
    saved: list[SectionRef] = []
    tool_call_count = state.get("tool_call_count", 0)
    failures = 0
    # 与 `tool` 节点同样的记账：章节写入同样是 `tool_calls` 行，它们归属于
    # 下面写出的 `write` 步骤。`replace` 按引用复制列表，因此注册表追加
    # 进去的正是运行上下文持有的同一个列表。
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
    # 只存了一部分的草稿不是完成的草稿：报告为待人工处理，而不是让一个章节数少于
    # 模板要求的运行看起来已经完成。
    next_prompt_tokens = prompt_tokens
    next_completion_tokens = completion_tokens
    next_cost = cost
    token_total = next_prompt_tokens + next_completion_tokens
    max_cost = state.get("max_cost", Decimal("0"))
    # 草稿调用和其他调用一样计费，所以它的费用也必须在这里加上：
    # 过去一次规划很便宜、起草很昂贵的运行，只会记下规划部分的费用。
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
    """只有在预算尚未耗尽且还有下一步时，才继续研究。"""
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
    """编译有界研究图，并把依赖绑定到各节点上。"""

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


#: 按执行顺序排列的节点名，供测试与文档使用。
GRAPH_NODES: tuple[str, ...] = ("plan", "tool", "observe", "verify", "write")
