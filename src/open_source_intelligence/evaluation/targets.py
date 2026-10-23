"""The five evaluation targets: one baseline, the full Agent, and three ablations.

Every target turns one `eval_cases` row into the same `VariantOutput` shape, which is what
makes the scorer and the runner target-agnostic. Two implementation choices carry the weight:

* The Agent targets call the real `run_research_agent` with an `ExecutionProfile`. An ablation
  is therefore the production pipeline minus one capability, not a reimplementation of it, so
  the measured drop is attributable to the capability rather than to different code.
* The baseline is a single model call over a prose digest of the candidate repositories. It
  gets the same output contract as the Agent (the same draft prompt and the same payload
  parser), but no tools, so any source it cites would have to come from the prompt alone.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.llm.base import ModelClient
from open_source_intelligence.agent.llm.errors import ModelClientError
from open_source_intelligence.agent.llm.factory import build_model_client
from open_source_intelligence.agent.llm.messages import (
    CANDIDATE_TAG,
    TOPIC_TAG,
    WINDOW_TAG,
    ChatMessage,
)
from open_source_intelligence.agent.profile import (
    READ_TOOLS,
    ExecutionProfile,
    profile_for_target,
)
from open_source_intelligence.agent.prompts import load_prompt
from open_source_intelligence.agent.runtime import load_candidate_repo_ids, run_research_agent
from open_source_intelligence.agent.tools.contracts import CitationRef
from open_source_intelligence.agent.writing import DraftPayloadError, parse_draft_payload
from open_source_intelligence.core.config import Settings
from open_source_intelligence.persistence.models.agent import ToolCall
from open_source_intelligence.persistence.models.evaluation import EvalCase
from open_source_intelligence.persistence.models.intelligence import (
    Repository,
    RepoSnapshot,
    Topic,
)
from open_source_intelligence.persistence.models.reporting import ReportDraft, ReportSection

#: The one non-Agent target; the other four all run the research graph.
BASELINE_TARGET: Final[str] = "baseline_single_prompt"
AGENT_TARGET: Final[str] = "agent_full"

#: Error codes recorded on a failed case result. These are evaluation outcomes, not API errors.
ERROR_TOPIC_NOT_FOUND: Final[str] = "EVAL_TOPIC_NOT_FOUND"
ERROR_CASE_INVALID: Final[str] = "EVAL_CASE_INVALID"
ERROR_PAYLOAD_INVALID: Final[str] = "EVAL_PAYLOAD_INVALID"
ERROR_MODEL_UNAVAILABLE: Final[str] = "EVAL_MODEL_UNAVAILABLE"

MAX_DIGEST_REPOS: Final[int] = 20
MAX_NOTES: Final[int] = 10

#: Read-only tools, as a set, so a tool-call count can be split into read and write calls.
READ_TOOL_NAMES: Final[frozenset[str]] = frozenset(READ_TOOLS)


@dataclass(frozen=True, slots=True)
class SectionDraft:
    """One drafted section plus the citations it claims."""

    section_key: str
    content_markdown: str
    citations: tuple[CitationRef, ...]


@dataclass(frozen=True, slots=True)
class VariantOutput:
    """One target's output for one case, in the shape every scorer consumes."""

    target_type: str
    case_key: str
    status: str
    error_code: str | None = None
    error_message: str | None = None
    topic_id: int | None = None
    sections: tuple[SectionDraft, ...] = ()
    tool_call_total: int = 0
    tool_call_succeeded: int = 0
    tool_call_denied: int = 0
    tool_call_read: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: Decimal = Decimal("0")
    latency_ms: int = 0
    guard_triggered: bool = False
    verification_sufficient: bool | None = None
    agent_run_id: int | None = None
    notes: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        """Return whether the target produced a scoreable run."""
        return self.status == "succeeded"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe summary for the run report."""
        return {
            "target_type": self.target_type,
            "case_key": self.case_key,
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "topic_id": self.topic_id,
            "section_count": len(self.sections),
            "citation_count": sum(len(section.citations) for section in self.sections),
            "tool_call_total": self.tool_call_total,
            "tool_call_succeeded": self.tool_call_succeeded,
            "tool_call_denied": self.tool_call_denied,
            "tool_call_read": self.tool_call_read,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "cost": str(self.cost),
            "latency_ms": self.latency_ms,
            "guard_triggered": self.guard_triggered,
            "verification_sufficient": self.verification_sufficient,
            "agent_run_id": self.agent_run_id,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class CaseContext:
    """The run context a case carries, after validation."""

    topic_name: str
    window_start: datetime
    window_end: datetime
    tags: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


def _parse_case_context(case: EvalCase) -> CaseContext:
    """Validate a stored `input_json` into a typed run context."""
    payload = case.input_json or {}
    topic_name = payload.get("topic")
    window = payload.get("window")
    if not isinstance(topic_name, str) or not topic_name.strip():
        raise ValueError(f"case {case.case_key} has no topic")
    if not isinstance(window, dict):
        raise ValueError(f"case {case.case_key} has no window")
    try:
        start = datetime.fromisoformat(str(window["start"]))
        end = datetime.fromisoformat(str(window["end"]))
    except (KeyError, ValueError) as exc:
        raise ValueError(f"case {case.case_key} has an invalid window") from exc
    raw_tags = payload.get("tags")
    tags = (
        tuple(item for item in raw_tags if isinstance(item, str))
        if isinstance(raw_tags, list)
        else ()
    )
    return CaseContext(topic_name=topic_name.strip(), window_start=start, window_end=end, tags=tags)


def resolve_case_topic(session: Session, *, topic_name: str) -> Topic | None:
    """Return the enabled topic a case refers to by name."""
    return session.scalar(
        select(Topic)
        .where(Topic.name == topic_name, Topic.enabled.is_(True))
        .order_by(Topic.id.asc())
        .limit(1)
    )


def _latest_snapshot(session: Session, repository_id: int) -> RepoSnapshot | None:
    """Return the newest snapshot of one repository."""
    return session.scalar(
        select(RepoSnapshot)
        .where(RepoSnapshot.repository_id == repository_id)
        .order_by(RepoSnapshot.snapshot_date.desc(), RepoSnapshot.id.desc())
        .limit(1)
    )


def _window_points(
    session: Session,
    *,
    repository_id: int,
    window_start: datetime | None,
    window_end: datetime | None,
) -> list[RepoSnapshot]:
    """Return a repository's snapshots inside a window, oldest first."""
    statement = select(RepoSnapshot).where(RepoSnapshot.repository_id == repository_id)
    if window_start is not None:
        statement = statement.where(RepoSnapshot.snapshot_date >= window_start.date())
    if window_end is not None:
        statement = statement.where(RepoSnapshot.snapshot_date <= window_end.date())
    return list(session.scalars(statement.order_by(RepoSnapshot.snapshot_date.asc())).all())


def _describe_series(points: list[RepoSnapshot]) -> str:
    """Render one repository's window series the way `query_repo_metrics` reports it."""
    if not points:
        return "no snapshot"
    first = points[0]
    latest = points[-1]
    if len(points) == 1:
        return f"stars={latest.stars}, forks={latest.forks}, 观测点 1 个（{latest.snapshot_date}）"
    return (
        f"stars {first.stars}→{latest.stars}（净增 {latest.stars - first.stars}），"
        f"forks {first.forks}→{latest.forks}，"
        f"{first.snapshot_date} 至 {latest.snapshot_date} 共 {len(points)} 个观测点"
    )


def build_candidate_digest(
    session: Session,
    *,
    topic_id: int,
    limit: int,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> tuple[str, list[int]]:
    """Render the candidate repositories as the prose a single-prompt baseline reads.

    This is the whole input a baseline gets: no tools, no prior reports, no cached README. It
    makes the baseline a real single-prompt attempt while keeping the comparison honest about
    what it cannot know.

    The metrics are read over the same window the Agent queries. Handing the baseline "the
    latest snapshot" while the Agent reads a closed historical window would compare two
    different datasets and call the difference a capability gap.
    """
    candidate_ids = load_candidate_repo_ids(session, topic_id=topic_id, limit=limit)
    lines: list[str] = []
    for repo_id in candidate_ids[:MAX_DIGEST_REPOS]:
        repository = session.get(Repository, repo_id)
        if repository is None:
            continue
        if window_start is None and window_end is None:
            latest = _latest_snapshot(session, repo_id)
            metrics = (
                f"stars={latest.stars}, forks={latest.forks}, open_issues={latest.open_issues}"
                if latest is not None
                else "no snapshot"
            )
        else:
            metrics = _describe_series(
                _window_points(
                    session,
                    repository_id=repo_id,
                    window_start=window_start,
                    window_end=window_end,
                )
            )
        language = repository.language or "unknown"
        lines.append(
            f"- {repository.full_name}（{language}，{metrics}）"
            f"：{repository.description or '无描述'}"
        )
    if not lines:
        lines.append("- 本期没有可用的候选仓库数据。")
    return "\n".join(lines), candidate_ids


def _baseline_messages(
    *,
    topic_id: int,
    candidate_digest: str,
    candidate_ids: list[int],
    window_start: datetime,
    window_end: datetime,
    draft_prompt: str,
) -> list[ChatMessage]:
    """Build the single-prompt baseline conversation."""
    window = json.dumps(
        {"start": window_start.isoformat(), "end": window_end.isoformat()},
        ensure_ascii=False,
    )
    user = "\n".join(
        [
            f"主题编号：{topic_id}",
            f"本轮候选仓库数量：{len(candidate_ids)}",
            "",
            "以下是本期候选仓库的汇总数据，请据此直接撰写周报：",
            candidate_digest,
            "",
            "机器可读上下文：",
            f"<{TOPIC_TAG}>{topic_id}</{TOPIC_TAG}>",
            f"<{CANDIDATE_TAG}>{json.dumps(candidate_ids)}</{CANDIDATE_TAG}>",
            f"<{WINDOW_TAG}>{window}</{WINDOW_TAG}>",
        ]
    )
    messages: list[ChatMessage] = []
    if draft_prompt:
        messages.append(ChatMessage(role="system", content=draft_prompt))
    messages.append(ChatMessage(role="user", content=user))
    return messages


def run_baseline(
    session: Session,
    *,
    case: EvalCase,
    settings: Settings,
    client: ModelClient | None = None,
    now: datetime | None = None,
) -> VariantOutput:
    """Run the single-prompt baseline over one case."""
    del now  # the baseline does not persist a run, so it has no clock to pin
    try:
        context = _parse_case_context(case)
    except ValueError as exc:
        return VariantOutput(
            target_type=BASELINE_TARGET,
            case_key=case.case_key,
            status="failed",
            error_code=ERROR_CASE_INVALID,
            error_message=str(exc),
        )
    topic = resolve_case_topic(session, topic_name=context.topic_name)
    if topic is None:
        return VariantOutput(
            target_type=BASELINE_TARGET,
            case_key=case.case_key,
            status="failed",
            error_code=ERROR_TOPIC_NOT_FOUND,
            error_message=f"topic {context.topic_name!r} is missing or disabled",
        )

    digest, candidate_ids = build_candidate_digest(
        session,
        topic_id=topic.id,
        limit=settings.agent_max_candidates,
        window_start=context.window_start,
        window_end=context.window_end,
    )
    resolved_client = client or build_model_client(settings)
    draft_prompt = load_prompt(
        "draft",
        settings.agent_prompt_version,
        settings.agent_prompts_dir,
    )
    messages = _baseline_messages(
        topic_id=topic.id,
        candidate_digest=digest,
        candidate_ids=candidate_ids,
        window_start=context.window_start,
        window_end=context.window_end,
        draft_prompt=draft_prompt,
    )
    started = time.perf_counter()
    try:
        turn = resolved_client.complete(messages, ())
    except ModelClientError as exc:
        return VariantOutput(
            target_type=BASELINE_TARGET,
            case_key=case.case_key,
            status="failed",
            error_code=ERROR_MODEL_UNAVAILABLE,
            error_message=exc.message,
            topic_id=topic.id,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    latency_ms = max(0, int((time.perf_counter() - started) * 1000))
    try:
        sections = parse_draft_payload(turn.text)
    except DraftPayloadError as exc:
        return VariantOutput(
            target_type=BASELINE_TARGET,
            case_key=case.case_key,
            status="failed",
            error_code=ERROR_PAYLOAD_INVALID,
            error_message=str(exc),
            topic_id=topic.id,
            prompt_tokens=turn.prompt_tokens,
            completion_tokens=turn.completion_tokens,
            cost=turn.cost,
            latency_ms=latency_ms,
        )
    return VariantOutput(
        target_type=BASELINE_TARGET,
        case_key=case.case_key,
        status="succeeded",
        topic_id=topic.id,
        sections=tuple(
            SectionDraft(
                section_key=section.section_key,
                content_markdown=section.content_markdown,
                citations=tuple(section.citations),
            )
            for section in sections
        ),
        prompt_tokens=turn.prompt_tokens,
        completion_tokens=turn.completion_tokens,
        cost=turn.cost,
        latency_ms=latency_ms,
        notes=("single prompt baseline: no tool observations are available to cite",),
    )


def load_draft_sections(session: Session, *, agent_run_id: int) -> tuple[SectionDraft, ...]:
    """Return the latest version of each section the Agent stored."""
    draft = session.scalar(select(ReportDraft).where(ReportDraft.agent_run_id == agent_run_id))
    if draft is None:
        return ()
    rows = session.scalars(
        select(ReportSection)
        .where(ReportSection.report_draft_id == draft.id)
        .order_by(ReportSection.section_key.asc(), ReportSection.version.desc())
    ).all()
    latest: dict[str, ReportSection] = {}
    for row in rows:
        latest.setdefault(row.section_key, row)
    drafts: list[SectionDraft] = []
    for row in latest.values():
        try:
            citations = tuple(
                CitationRef.model_validate(item) for item in (row.citations_json or [])
            )
        except ValueError:
            citations = ()
        drafts.append(
            SectionDraft(
                section_key=row.section_key,
                content_markdown=row.content_markdown,
                citations=citations,
            )
        )
    return tuple(sorted(drafts, key=lambda item: item.section_key))


def load_tool_stats(session: Session, *, agent_run_id: int) -> tuple[int, int, int, int]:
    """Return `(total, succeeded, denied, read_only)` tool-call counts for one agent run.

    Read-only calls are counted separately because they are the only ones that can pull
    untrusted external text into the conversation: the write tool stores a draft the model
    already produced. An injection metric that counted writes would report a run with no
    content reads as though it had an injection surface.
    """
    rows = session.execute(
        select(ToolCall.tool_name, ToolCall.status).where(ToolCall.agent_run_id == agent_run_id)
    ).all()
    statuses = [str(row[1]) for row in rows]
    read_only = sum(1 for row in rows if str(row[0]) in READ_TOOL_NAMES)
    return len(statuses), statuses.count("success"), statuses.count("denied"), read_only


def run_agent_target(
    session: Session,
    *,
    case: EvalCase,
    target_type: str,
    settings: Settings,
    client: ModelClient | None = None,
    now: datetime | None = None,
) -> VariantOutput:
    """Run the full Agent or one of its ablations over a single case."""
    profile: ExecutionProfile | None = profile_for_target(target_type)
    if profile is None:
        raise ValueError(f"{target_type} is not an agent target")
    try:
        context = _parse_case_context(case)
    except ValueError as exc:
        return VariantOutput(
            target_type=target_type,
            case_key=case.case_key,
            status="failed",
            error_code=ERROR_CASE_INVALID,
            error_message=str(exc),
        )
    topic = resolve_case_topic(session, topic_name=context.topic_name)
    if topic is None:
        return VariantOutput(
            target_type=target_type,
            case_key=case.case_key,
            status="failed",
            error_code=ERROR_TOPIC_NOT_FOUND,
            error_message=f"topic {context.topic_name!r} is missing or disabled",
        )

    started = time.perf_counter()
    try:
        result = run_research_agent(
            session,
            topic_id=topic.id,
            settings=settings,
            client=client,
            window_start=context.window_start,
            window_end=context.window_end,
            now=now,
            profile=profile,
        )
    except ModelClientError as exc:
        return VariantOutput(
            target_type=target_type,
            case_key=case.case_key,
            status="failed",
            error_code=ERROR_MODEL_UNAVAILABLE,
            error_message=exc.message,
            topic_id=topic.id,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
        )
    latency_ms = max(0, int((time.perf_counter() - started) * 1000))
    total, succeeded, denied, read_only = load_tool_stats(session, agent_run_id=result.agent_run_id)
    verification = result.verification
    notes: list[str] = []
    if verification is not None and not verification.sufficient:
        notes.append(f"verification insufficient: {verification.reason}")
    # An agent run that only reached `needs_review` still produced a draft, and scoring that
    # draft is exactly how the ablation's damage shows up. Only a genuinely failed run is
    # reported as failed, because there is nothing to score.
    status = "failed" if result.status == "failed" else "succeeded"
    return VariantOutput(
        target_type=target_type,
        case_key=case.case_key,
        status=status,
        error_code=result.error_code,
        topic_id=topic.id,
        sections=load_draft_sections(session, agent_run_id=result.agent_run_id),
        tool_call_total=total,
        tool_call_succeeded=succeeded,
        tool_call_denied=denied,
        tool_call_read=read_only,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cost=result.estimated_cost,
        latency_ms=latency_ms,
        guard_triggered=result.guard_triggered,
        verification_sufficient=None if verification is None else verification.sufficient,
        agent_run_id=result.agent_run_id,
        notes=tuple(notes[:MAX_NOTES]),
    )


def run_target(
    session: Session,
    *,
    case: EvalCase,
    target_type: str,
    settings: Settings,
    client: ModelClient | None = None,
    now: datetime | None = None,
) -> VariantOutput:
    """Dispatch one case to the requested target variant."""
    if target_type == BASELINE_TARGET:
        return run_baseline(session, case=case, settings=settings, client=client, now=now)
    if profile_for_target(target_type) is not None:
        return run_agent_target(
            session,
            case=case,
            target_type=target_type,
            settings=settings,
            client=client,
            now=now,
        )
    raise ValueError(f"unsupported eval target: {target_type}")


#: The baseline first, then the full Agent, then the ablations - the order the report reads in.
TARGET_ORDER: Final[tuple[str, ...]] = (
    BASELINE_TARGET,
    AGENT_TARGET,
    "agent_no_verifier",
    "agent_no_tools",
    "agent_no_history",
)
