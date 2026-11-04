"""提供五个评测目标：一个基线、完整的 Agent 与三个消融变体。

每个目标都把一行 `eval_cases` 转成同一个 `VariantOutput` 形状，正因如此，打分器和运行器
才能与具体目标无关。有两个实现选择承载着分量：

* Agent 类的目标会带 `ExecutionProfile` 调用真正的 `run_research_agent`。因此一个消融变体
  就是生产链路减掉一项能力，而不是它的另一套实现，测出来的下降才能归因于那项能力，而不是
  归因于不同的代码。
* 基线是对候选仓库的散文摘要做一次模型调用。它拿到与 Agent 相同的输出契约（同一份草稿
  Prompt、同一个载荷解析器），但没有工具，所以它引用的任何来源都只能来自 Prompt 本身。
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

#: 唯一一个非 Agent 目标；其余四个都会跑研究图。
BASELINE_TARGET: Final[str] = "baseline_single_prompt"
AGENT_TARGET: Final[str] = "agent_full"

#: 失败用例结果上记录的错误码。这些是评测结论，不是 API 错误。
ERROR_TOPIC_NOT_FOUND: Final[str] = "EVAL_TOPIC_NOT_FOUND"
ERROR_CASE_INVALID: Final[str] = "EVAL_CASE_INVALID"
ERROR_PAYLOAD_INVALID: Final[str] = "EVAL_PAYLOAD_INVALID"
ERROR_MODEL_UNAVAILABLE: Final[str] = "EVAL_MODEL_UNAVAILABLE"

MAX_DIGEST_REPOS: Final[int] = 20
MAX_NOTES: Final[int] = 10

#: 只读工具的集合，便于把工具调用计数拆成读调用与写调用。
READ_TOOL_NAMES: Final[frozenset[str]] = frozenset(READ_TOOLS)


@dataclass(frozen=True, slots=True)
class SectionDraft:
    """一个起草好的章节，加上它声称的引用。"""

    section_key: str
    content_markdown: str
    citations: tuple[CitationRef, ...]


@dataclass(frozen=True, slots=True)
class VariantOutput:
    """一个目标对一个用例的输出，形状即所有打分器所消费的那一种。"""

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
        """返回目标是否产出了可打分的一次运行。"""
        return self.status == "succeeded"

    def to_dict(self) -> dict[str, Any]:
        """返回可安全序列化为 JSON 的摘要，供运行报告使用。"""
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
    """用例携带的运行上下文，已通过校验。"""

    topic_name: str
    window_start: datetime
    window_end: datetime
    tags: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


def _parse_case_context(case: EvalCase) -> CaseContext:
    """把存储的 `input_json` 校验成带类型的运行上下文。"""
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
    """返回用例按名称所指的那个启用中的主题。"""
    return session.scalar(
        select(Topic)
        .where(Topic.name == topic_name, Topic.enabled.is_(True))
        .order_by(Topic.id.asc())
        .limit(1)
    )


def _latest_snapshot(session: Session, repository_id: int) -> RepoSnapshot | None:
    """返回某个仓库最新的一份快照。"""
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
    """返回某个仓库在窗口内的快照，最早的在前。"""
    statement = select(RepoSnapshot).where(RepoSnapshot.repository_id == repository_id)
    if window_start is not None:
        statement = statement.where(RepoSnapshot.snapshot_date >= window_start.date())
    if window_end is not None:
        statement = statement.where(RepoSnapshot.snapshot_date <= window_end.date())
    return list(session.scalars(statement.order_by(RepoSnapshot.snapshot_date.asc())).all())


def _describe_series(points: list[RepoSnapshot]) -> str:
    """按 `query_repo_metrics` 的汇报方式渲染一个仓库在窗口内的序列。"""
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
    """把候选仓库渲染成单 Prompt 基线所读的那段散文。

    这就是基线拿到的全部输入：没有工具、没有历史报告、没有缓存 README。它让基线成为一次
    真正意义上的单 Prompt 尝试，同时让对比对「基线不可能知道什么」保持诚实。

    指标读取的窗口与 Agent 查询的窗口相同。若给基线「最新的快照」而 Agent 读的是一个已
    封闭的历史窗口，比的就是两份不同的数据集，却把差异说成了能力差距。
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
    """构造单 Prompt 基线的对话。"""
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
    """在一个用例上运行单 Prompt 基线。"""
    del now  # 基线不落库运行记录，因此没有需要钉住的时钟
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
    """返回 Agent 存储的每个章节的最新版本。"""
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
    """返回某次 Agent 运行的 `(total, succeeded, denied, read_only)` 工具调用计数。

    只读调用单独计数，因为只有它们才能把不可信的外部文本拉进对话：写工具存下的只是模型
    已经产出的草稿。把写调用也算进去的注入指标，会让一次没有任何内容读取的运行看起来像
    是有注入面的。
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
    """在一个用例上运行完整 Agent 或它的某个消融变体。"""
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
    # 一次只走到 `needs_review` 的 Agent 运行仍然产出了草稿，而给这份草稿打分，正是消融
    # 变体所受损伤显形的方式。只有真正失败的运行才报成失败，因为它没有东西可打分。
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
    """把一个用例派发给所请求的目标变体。"""
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


#: 先是基线，然后是完整 Agent，最后是各消融变体 —— 也就是报告阅读的先后顺序。
TARGET_ORDER: Final[tuple[str, ...]] = (
    BASELINE_TARGET,
    AGENT_TARGET,
    "agent_no_verifier",
    "agent_no_tools",
    "agent_no_history",
)
