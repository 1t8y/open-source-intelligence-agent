"""Run one bounded research Agent over a topic window and persist the whole trail.

The runtime is the only place that decides what a run is allowed to touch: it resolves the
Agent config, picks the authorise-able candidate repositories, injects a trusted
`ToolContext`, and drives the graph. Nothing about authorization is reachable from model
arguments.
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
    AGENT_BUDGET_EXCEEDED,
    AGENT_EVIDENCE_INCOMPLETE,
    AGENT_INTERNAL_ERROR,
    AGENT_NO_CANDIDATES,
    AGENT_PROMPT_INJECTION,
    AGENT_WRITE_FAILED,
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

#: Name of the Agent configuration row the runtime creates when none exists yet.
DEFAULT_AGENT_CONFIG_NAME = "default-research-agent"

#: Error codes that mean "a human should look at this" rather than "the run broke".
REVIEW_REQUIRED_CODES = frozenset(
    {
        AGENT_BUDGET_EXCEEDED,
        AGENT_EVIDENCE_INCOMPLETE,
        AGENT_NO_CANDIDATES,
        AGENT_PROMPT_INJECTION,
        AGENT_WRITE_FAILED,
    }
)

#: Upper bound on the failure detail kept on `pipeline_runs.metrics_json`.
MAX_FAILURE_DETAIL_CHARS = 2000


@dataclass(slots=True)
class AgentRunResult:
    """Summary of one finished research run."""

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
        """Return a JSON-safe summary."""
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
    """Intersect the configured tool policy with the hard-coded whitelist.

    A policy can only ever shrink the whitelist. When the policy is absent or malformed the
    full whitelist applies, so a broken config cannot silently widen or silently disable tools.
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
    """Narrow an already-authorized tool set by an execution profile.

    Order matters: the config narrows the whitelist, and the profile narrows the result, so an
    ablation can only ever remove capability. `save_report_section` is re-added if the config
    had granted it, because an ablation that produced no draft at all would measure nothing.
    """
    allowed = tuple(name for name in configured_tools if profile.allows(name))
    if WRITE_TOOL in configured_tools and WRITE_TOOL not in allowed:
        allowed = (*allowed, WRITE_TOOL)
    return allowed


def _plan_schemas(
    registry: ToolRegistry,
    allowed_tools: tuple[str, ...],
) -> tuple[ToolSchema, ...]:
    """Return the schemas a run may plan with.

    Draft writing is never a planned action - the graph calls it directly once the research
    stage is over - so it is withheld from the model even when the run may use it.
    """
    return tuple(
        schema
        for schema in build_tool_schemas(registry)
        if schema.name in allowed_tools and schema.name != WRITE_TOOL
    )


def load_or_create_config(session: Session, settings: Settings) -> AgentConfig:
    """Return the enabled Agent config, creating the default row on first use."""
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
    """Write a terminal `failed` state for a run that blew up outside the graph's control.

    Without this, an exception escaping a node (a broken provider adapter, a database
    hiccup while recording a step) would leave `agent_runs` and `pipeline_runs` stuck in
    `running` forever, so operators could not tell a live run from a dead one. The original
    exception is always re-raised by the caller; if the recovery write itself fails, the
    session is rolled back rather than masking the original failure.
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
    except Exception:  # noqa: BLE001 - never mask the original failure
        session.rollback()


def load_candidate_repo_ids(session: Session, *, topic_id: int, limit: int) -> list[int]:
    """Return the ranked, Agent-selected repository ids for one topic."""
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
    """Resolve the research window, defaulting to the topic's trailing window."""
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
    """Render the machine-readable context block every provider reads."""
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
    """Decide the run's terminal status from the final graph state.

    Budget exhaustion and a missing evidence base are not crashes: they mean the draft needs
    a human. Only transport, model, and payload failures mark the run as failed.

    `completed` requires both a complete evidence base and no error code at all, so a run
    that hit a review-worthy problem (say a draft section that could not be stored) can never
    be reported as a clean completion.
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
    """Execute one bounded research run and return its summary.

    `profile` is the ablation switch the evaluation runner uses; a production run leaves it at
    `AGENT_FULL`, where it changes nothing.
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

    context = ToolContext(
        agent_run_id=agent_run.id,
        topic_id=topic_id,
        authorized_topic_ids=frozenset({topic_id}),
        allowed_tools=allowed_tools,
        max_tool_calls=config.max_tool_calls,
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
        "max_tool_calls": config.max_tool_calls,
        "max_tokens": config.max_tokens,
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
    except Exception as exc:  # noqa: BLE001 - the run must still reach a terminal state
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
