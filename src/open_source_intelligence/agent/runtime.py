"""在一个主题时间窗上运行一次有界研究 Agent，并持久化整条轨迹。

运行时是唯一决定一次运行可以碰什么的场所：它解析 Agent 配置、挑出可授权
的候选仓库、注入可信的 `ToolContext`，并驱动图。授权相关的任何东西都无法
从模型参数触达。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.errors import (
    AGENT_EVIDENCE_INCOMPLETE,
    AGENT_INTERNAL_ERROR,
    REVIEW_REQUIRED_CODES,
)
from open_source_intelligence.agent.graph import GraphDependencies, build_research_graph
from open_source_intelligence.agent.llm.base import ModelClient
from open_source_intelligence.agent.llm.factory import build_model_client
from open_source_intelligence.agent.llm.messages import (
    CANDIDATE_TAG,
    TOPIC_TAG,
    WINDOW_TAG,
    ChatMessage,
    ToolSchema,
)
from open_source_intelligence.agent.profile import AGENT_FULL, WRITE_TOOL, ExecutionProfile
from open_source_intelligence.agent.prompts import load_prompt
from open_source_intelligence.agent.recording import StepRecorder
from open_source_intelligence.agent.state import AgentState, VerificationResult
from open_source_intelligence.agent.tooling import build_tool_schemas
from open_source_intelligence.agent.tools.context import ToolContext
from open_source_intelligence.agent.tools.registry import (
    WHITELIST,
    ToolRegistry,
    build_default_registry,
)
from open_source_intelligence.core.config import Settings
from open_source_intelligence.core.logging import redact_text
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.agent import AgentConfig, AgentRun
from open_source_intelligence.persistence.models.intelligence import (
    PipelineRun,
    Topic,
    TopicRepository,
)

#: 尚不存在任何配置时，运行时创建的 Agent 配置行名称。
DEFAULT_AGENT_CONFIG_NAME = "default-research-agent"

#: `pipeline_runs.metrics_json` 上保留的失败详情长度上限。
MAX_FAILURE_DETAIL_CHARS = 2000


@dataclass(slots=True)
class AgentRunResult:
    """一次已完成研究运行的摘要。"""

    agent_run_id: int
    pipeline_run_id: int
    topic_id: int
    status: str
    error_code: str | None
    loop_count: int
    tool_call_count: int
    model_call_count: int
    prompt_tokens: int
    completion_tokens: int
    estimated_cost: Decimal
    budget_degraded: bool = False
    guard_triggered: bool = False
    candidate_repo_ids: list[int] = field(default_factory=list)
    evidence_count: int = 0
    section_count: int = 0
    report_draft_id: int | None = None
    verification: VerificationResult | None = None

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 安全的摘要。"""
        return {
            "agent_run_id": self.agent_run_id,
            "pipeline_run_id": self.pipeline_run_id,
            "topic_id": self.topic_id,
            "status": self.status,
            "error_code": self.error_code,
            "loop_count": self.loop_count,
            "tool_call_count": self.tool_call_count,
            "model_call_count": self.model_call_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "estimated_cost": str(self.estimated_cost),
            "budget_degraded": self.budget_degraded,
            "guard_triggered": self.guard_triggered,
            "candidate_repo_ids": self.candidate_repo_ids,
            "evidence_count": self.evidence_count,
            "section_count": self.section_count,
            "report_draft_id": self.report_draft_id,
            "verification": None if self.verification is None else self.verification.model_dump(),
        }


def resolve_allowed_tools(policy: dict[str, Any]) -> tuple[str, ...]:
    """把配置里的工具策略与硬编码白名单取交集。

    策略只可能收窄白名单。策略缺失或格式不对时，整份白名单生效，这样一份坏配置既
    无法悄悄放宽工具，也无法悄悄禁用工具。
    """
    requested = policy.get("tools")
    if not isinstance(requested, list):
        return WHITELIST
    names = {item for item in requested if isinstance(item, str)}
    return tuple(name for name in WHITELIST if name in names)


def apply_profile(
    configured_tools: tuple[str, ...],
    profile: ExecutionProfile,
) -> tuple[str, ...]:
    """用执行画像收窄一份已经授权的工具集合。

    顺序很重要：配置先收窄白名单，画像再收窄结果，因此消融只可能移除能力。若配置
    原本就授予了 `save_report_section`，它会被重新加回来，因为一次完全
    产不出草稿的消融什么都衡量不了。
    """
    allowed = tuple(name for name in configured_tools if profile.allows(name))
    if WRITE_TOOL in configured_tools and WRITE_TOOL not in allowed:
        allowed = (*allowed, WRITE_TOOL)
    return allowed


def _plan_schemas(
    registry: ToolRegistry,
    allowed_tools: tuple[str, ...],
) -> tuple[ToolSchema, ...]:
    """返回一次运行可以用来做规划的那些 schema。

    起草从来不是规划动作 —— 研究阶段结束后由图直接调用 —— 所以即使运行有权使用
    它，也不会把它交给模型。
    """
    return tuple(
        schema
        for schema in build_tool_schemas(registry)
        if schema.name in allowed_tools and schema.name != WRITE_TOOL
    )


def evidence_tool_floor(candidate_count: int) -> int:
    """返回验证器有可能通过之前，一次运行所需的最少工具调用数。

    验证器要求三类证据（`graph._verify_node`）：每个候选的画像**和**指标、
    至少一份缓存的 README、至少一次历史报告查询。也就是 `2 x 候选数 + 2`
    次调用，任何低于它的预算都会产出一个无论模型推理多好都不可能成功的运行。

    它存在的原因是这两个数字曾各自独立配置并逐渐脱节：默认候选上限是 5，而默认
    预算曾是 10，于是每次定时运行都把最后一次调用花在第十次画像/指标查询上，从不
    读 README，记下 `AGENT_EVIDENCE_INCOMPLETE`，也不产出报告 —— 每天一次，
    悄无声息。工具调用不花 token（只有请求它们的模型轮次才花），所以这个下限是
    把预算抬上去，而不是把候选集缩小：后一种做法会悄悄缩窄报告的覆盖面。
    """
    return 2 * max(0, candidate_count) + 2


def load_or_create_config(session: Session, settings: Settings) -> AgentConfig:
    """返回已启用的 Agent 配置，首次使用时创建默认行。"""
    config = session.scalar(
        select(AgentConfig)
        .where(AgentConfig.name == DEFAULT_AGENT_CONFIG_NAME, AgentConfig.enabled.is_(True))
        .order_by(AgentConfig.id.desc())
        .limit(1)
    )
    if config is not None:
        return config
    config = AgentConfig(
        name=DEFAULT_AGENT_CONFIG_NAME,
        version=settings.agent_prompt_version,
        provider=settings.agent_provider,
        model=settings.agent_model,
        prompt_version=settings.agent_prompt_version,
        tool_policy_json={"tools": list(WHITELIST)},
        max_loops=settings.agent_max_loops,
        max_model_calls=settings.agent_max_model_calls,
        max_tool_calls=settings.agent_max_tool_calls,
        max_tokens=settings.agent_max_tokens,
        max_cost=settings.agent_max_cost,
        enabled=True,
    )
    session.add(config)
    session.commit()
    return config


def _record_crashed_run(
    session: Session,
    recorder: StepRecorder,
    pipeline_run: PipelineRun,
    error: BaseException,
) -> None:
    """为在图控制之外炸掉的运行写入终态 `failed`。

    没有它，从节点里逃出来的异常（供应商适配器坏了、记录步骤时数据库抖了
    一下）会让 `agent_runs` 与 `pipeline_runs` 永远停在 `running`，运维人员
    就分不清运行是活着还是死了。原始异常始终由调用方重新抛出；若补救写入
    本身也失败，则回滚会话，而不是掩盖原始失败。
    """
    try:
        recorder.finish(status="failed", error_code=AGENT_INTERNAL_ERROR)
        pipeline_run.status = "failed"
        pipeline_run.finished_at = utc_now()
        pipeline_run.error_code = AGENT_INTERNAL_ERROR
        pipeline_run.metrics_json = {
            "error_code": AGENT_INTERNAL_ERROR,
            "error_message": redact_text(str(error)).strip()[:MAX_FAILURE_DETAIL_CHARS]
            or error.__class__.__name__,
        }
        session.commit()
    except Exception:  # noqa: BLE001 - 绝不掩盖原始失败
        session.rollback()


def load_candidate_repo_ids(session: Session, *, topic_id: int, limit: int) -> list[int]:
    """返回某个主题下按排名排序、由 Agent 选中的仓库 id。"""
    rows = session.scalars(
        select(TopicRepository.repository_id)
        .where(
            TopicRepository.topic_id == topic_id,
            TopicRepository.selected_for_agent.is_(True),
        )
        .order_by(TopicRepository.rank.asc())
        .limit(limit)
    ).all()
    return [int(row) for row in rows]


def resolve_window(
    topic: Topic,
    *,
    window_start: datetime | None,
    window_end: datetime | None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """解析研究时间窗，默认取该主题的滚动窗口。"""
    resolved_end = window_end or now or utc_now()
    resolved_start = window_start or (resolved_end - timedelta(days=topic.window_days))
    if resolved_start > resolved_end:
        raise ValueError("window_start must not be after window_end")
    return resolved_start, resolved_end


def build_user_context(
    *,
    topic_id: int,
    candidate_repo_ids: list[int],
    window_start: datetime,
    window_end: datetime,
) -> str:
    """渲染每个供应商都会读到的机器可读上下文块。"""
    window = json.dumps(
        {"start": window_start.isoformat(), "end": window_end.isoformat()},
        ensure_ascii=False,
    )
    return "\n".join(
        [
            f"主题编号：{topic_id}",
            f"本轮候选仓库数量：{len(candidate_repo_ids)}",
            "",
            "请依据以下机器可读区块开展研究：",
            f"<{TOPIC_TAG}>{topic_id}</{TOPIC_TAG}>",
            f"<{CANDIDATE_TAG}>{json.dumps(candidate_repo_ids)}</{CANDIDATE_TAG}>",
            f"<{WINDOW_TAG}>{window}</{WINDOW_TAG}>",
        ]
    )


def _terminal_status(state: AgentState) -> tuple[str, str | None]:
    """由图的最终状态判定运行的终态。

    预算耗尽与证据基础缺失都不是崩溃：它们意味着草稿需要人工。只有传输、模型与
    载荷失败才把运行标记为 failed。

    `completed` 同时要求证据基础完整且完全没有错误码，因此撞上过值得复核的问题的
    运行（比如某个草稿章节存不下来），永远不会被报告成一次干净的完成。
    """
    error_code = state.get("error_code")
    verification = state.get("verification")
    sufficient = verification is not None and verification.sufficient
    if error_code is None and sufficient:
        return "completed", None
    if error_code is not None and error_code not in REVIEW_REQUIRED_CODES:
        return "failed", error_code
    return "needs_review", error_code or AGENT_EVIDENCE_INCOMPLETE


def run_research_agent(
    session: Session,
    *,
    topic_id: int,
    settings: Settings,
    client: ModelClient | None = None,
    registry: ToolRegistry | None = None,
    pipeline_run_id: int | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    max_candidates: int | None = None,
    now: datetime | None = None,
    profile: ExecutionProfile = AGENT_FULL,
) -> AgentRunResult:
    """执行一次有界研究运行并返回其摘要。

    `profile` 是评测运行器使用的消融开关；生产运行把它留在 `AGENT_FULL`，
    在那里它什么都不改变。
    """
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise ValueError(f"topic {topic_id} does not exist")

    resolved_start, resolved_end = resolve_window(
        topic,
        window_start=window_start,
        window_end=window_end,
        now=now,
    )
    candidate_ids = load_candidate_repo_ids(
        session,
        topic_id=topic_id,
        limit=max_candidates or settings.agent_max_candidates,
    )
    config = load_or_create_config(session, settings)
    allowed_tools = apply_profile(resolve_allowed_tools(config.tool_policy_json), profile)
    resolved_client = client or build_model_client(settings)
    resolved_registry = registry or build_default_registry()

    pipeline_run: PipelineRun
    if pipeline_run_id is None:
        pipeline_run = PipelineRun(
            topic_id=topic_id,
            run_type="agent",
            status="running",
            started_at=utc_now(),
            attempt=1,
        )
        session.add(pipeline_run)
        session.commit()
    else:
        existing_run = session.get(PipelineRun, pipeline_run_id)
        if existing_run is None:
            raise ValueError(f"pipeline run {pipeline_run_id} does not exist")
        pipeline_run = existing_run

    agent_run = AgentRun(
        pipeline_run_id=pipeline_run.id,
        topic_id=topic_id,
        agent_config_id=config.id,
        status="pending",
        window_start=resolved_start,
        window_end=resolved_end,
    )
    session.add(agent_run)
    session.commit()

    recorder = StepRecorder(session, agent_run)
    recorder.mark_running()

    # 配置的上限是天花板而非目标：证据门槛会在它下面设一个下限。
    tool_budget = max(config.max_tool_calls, evidence_tool_floor(len(candidate_ids)))
    context = ToolContext(
        agent_run_id=agent_run.id,
        topic_id=topic_id,
        authorized_topic_ids=frozenset({topic_id}),
        allowed_tools=allowed_tools,
        max_tool_calls=tool_budget,
    )
    deps = GraphDependencies(
        client=resolved_client,
        registry=resolved_registry,
        session=session,
        recorder=recorder,
        context=context,
        draft_prompt=load_prompt("draft", config.prompt_version, settings.agent_prompts_dir),
        tool_schemas=_plan_schemas(resolved_registry, allowed_tools),
        run_verifier=profile.run_verifier,
    )
    graph = build_research_graph(deps)
    initial_state: AgentState = {
        "run_id": agent_run.id,
        "topic_id": topic_id,
        "window_start": resolved_start,
        "window_end": resolved_end,
        "candidate_repo_ids": candidate_ids,
        "prompt_version": config.prompt_version,
        "max_loops": config.max_loops,
        "max_model_calls": config.max_model_calls,
        "max_tool_calls": tool_budget,
        "max_tokens": config.max_tokens,
        "max_cost": config.max_cost,
        "research_plan": [],
        "evidence": [],
        "draft_sections": [],
        "messages": [
            ChatMessage(
                role="system",
                content=load_prompt("plan", config.prompt_version, settings.agent_prompts_dir),
            ),
            ChatMessage(
                role="user",
                content=build_user_context(
                    topic_id=topic_id,
                    candidate_repo_ids=candidate_ids,
                    window_start=resolved_start,
                    window_end=resolved_end,
                ),
            ),
        ],
        "last_observations": [],
        "pending_tool_calls": [],
        "verification": None,
        "loop_count": 0,
        "tool_call_count": 0,
        "model_call_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "estimated_cost": Decimal("0"),
        "budget_degraded": False,
        "guard_triggered": False,
        "status": "running",
        "error_code": None,
        "error_message": None,
        "incomplete": False,
    }
    try:
        final_state = cast(AgentState, graph.invoke(initial_state))
    except Exception as exc:  # noqa: BLE001 - 运行必须到达终态
        _record_crashed_run(session, recorder, pipeline_run, exc)
        raise

    recorder.sync_counters(
        loop_count=final_state.get("loop_count", 0),
        tool_call_count=final_state.get("tool_call_count", 0),
        model_call_count=final_state.get("model_call_count", 0),
        prompt_tokens=final_state.get("prompt_tokens", 0),
        completion_tokens=final_state.get("completion_tokens", 0),
        estimated_cost=final_state.get("estimated_cost", Decimal("0")),
    )
    status, error_code = _terminal_status(final_state)
    recorder.finish(status=status, error_code=error_code)

    sections = list(final_state.get("draft_sections") or [])
    verification = final_state.get("verification")
    result = AgentRunResult(
        agent_run_id=agent_run.id,
        pipeline_run_id=pipeline_run.id,
        topic_id=topic_id,
        status=status,
        error_code=error_code,
        loop_count=final_state.get("loop_count", 0),
        tool_call_count=final_state.get("tool_call_count", 0),
        model_call_count=final_state.get("model_call_count", 0),
        prompt_tokens=final_state.get("prompt_tokens", 0),
        completion_tokens=final_state.get("completion_tokens", 0),
        estimated_cost=final_state.get("estimated_cost", Decimal("0")),
        budget_degraded=bool(final_state.get("budget_degraded")),
        guard_triggered=bool(final_state.get("guard_triggered")),
        candidate_repo_ids=candidate_ids,
        evidence_count=len(final_state.get("evidence") or []),
        section_count=len(sections),
        report_draft_id=None if not sections else sections[0].report_draft_id,
        verification=verification,
    )
    pipeline_run.status = "succeeded" if status == "completed" else "failed"
    pipeline_run.finished_at = utc_now()
    pipeline_run.error_code = error_code
    pipeline_run.metrics_json = result.to_dict()
    session.commit()
    return result
