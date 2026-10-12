"""Deterministic offline model client used for local runs and evaluation baselines.

The stub performs no network calls. It reads exactly the same prompt context a remote
model receives, then advances one fixed research step per turn:

    profile -> metrics -> prior reports -> content -> final

Because the policy is a pure function of the conversation, two runs over the same
inputs produce byte-identical turns. That makes it a usable evaluation baseline and
lets the whole graph run on a machine without outbound network access.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from typing import Any, TypeGuard

from open_source_intelligence.agent.tools.contracts import (
    MAX_PRIOR_REPORT_LIMIT,
    REPORT_SECTION_KEYS,
    CitationRef,
)

from .messages import (
    CANDIDATE_TAG,
    TOPIC_TAG,
    WINDOW_TAG,
    ChatMessage,
    ModelToolCall,
    ModelTurn,
    ToolSchema,
)

DEFAULT_OFFLINE_MODEL = "offline-deterministic-v1"

#: Deep README review is the most expensive step, so the stub spends it on the top candidates.
DEFAULT_CONTENT_REPO_LIMIT = 2
DEFAULT_PRIOR_REPORT_LIMIT = 5


@cache
def _block_pattern(tag: str) -> re.Pattern[str]:
    """Return the cached non-greedy matcher for one context tag."""
    return re.compile(rf"<{tag}>(.*?)</{tag}>", re.DOTALL)


def _extract_tag(messages: Sequence[ChatMessage], tag: str) -> str | None:
    """Return the first user context block with the given tag.

    Only user messages are inspected. The system prompt documents the block syntax with
    placeholder examples, and scanning it would let those placeholders masquerade as real
    run context.
    """
    pattern = _block_pattern(tag)
    for message in messages:
        if message.role != "user":
            continue
        match = pattern.search(message.content)
        if match is not None:
            return match.group(1).strip()
    return None


def _is_real_int(value: object) -> TypeGuard[int]:
    """Return whether a JSON value is an integer and not a boolean."""
    return isinstance(value, int) and not isinstance(value, bool)


def extract_candidate_ids(messages: Sequence[ChatMessage]) -> tuple[int, ...]:
    """Return the candidate repository ids advertised in the prompt context."""
    raw = _extract_tag(messages, CANDIDATE_TAG)
    if raw is None:
        return ()
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, list):
        return ()
    return tuple(item for item in payload if _is_real_int(item))


def extract_topic_id(messages: Sequence[ChatMessage]) -> int | None:
    """Return the topic id advertised in the prompt context."""
    raw = _extract_tag(messages, TOPIC_TAG)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def extract_window(messages: Sequence[ChatMessage]) -> tuple[datetime, datetime] | None:
    """Return the research window advertised in the prompt context."""
    raw = _extract_tag(messages, WINDOW_TAG)
    if raw is None:
        return None
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    start = payload.get("start")
    end = payload.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        return datetime.fromisoformat(start), datetime.fromisoformat(end)
    except ValueError:
        return None


def requested_tool_calls(messages: Sequence[ChatMessage]) -> set[tuple[str, int | None]]:
    """Return every `(tool_name, repo_id)` pair the assistant already asked for."""
    requested: set[tuple[str, int | None]] = set()
    for message in messages:
        if message.role != "assistant":
            continue
        for call in message.tool_calls:
            repo_id = call.arguments.get("repo_id")
            requested.add((call.name, repo_id if _is_real_int(repo_id) else None))
    return requested


def _arguments_by_call_id(messages: Sequence[ChatMessage]) -> dict[str, dict[str, Any]]:
    """Map each tool call id back to the arguments the assistant supplied."""
    mapping: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.role != "assistant":
            continue
        for call in message.tool_calls:
            mapping[call.call_id] = call.arguments
    return mapping


@dataclass(slots=True)
class _Evidence:
    """Tool observations re-assembled into a comparable snapshot."""

    profiles: dict[int, dict[str, Any]] = field(default_factory=dict)
    metrics: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    contents: dict[int, dict[str, Any]] = field(default_factory=dict)
    prior_reports: list[dict[str, Any]] = field(default_factory=list)

    def repo_ids(self) -> list[int]:
        """Return every repository id seen across all observation types."""
        seen = set(self.profiles) | set(self.metrics) | set(self.contents)
        return sorted(seen)


def _int_field(payload: dict[str, Any], key: str) -> int:
    """Read an integer field defensively, treating anything else as zero."""
    value = payload.get(key)
    if not _is_real_int(value):
        return 0
    return value


def _net_star_delta(points: Sequence[dict[str, Any]]) -> int:
    """Return last-minus-first stars across an ordered metric series."""
    if len(points) < 2:
        return 0
    return _int_field(points[-1], "stars") - _int_field(points[0], "stars")


def _collect_evidence(messages: Sequence[ChatMessage]) -> _Evidence:
    """Rebuild the research snapshot from recorded tool observations."""
    evidence = _Evidence()
    arguments_by_call = _arguments_by_call_id(messages)
    for message in messages:
        if message.role != "tool" or not message.content:
            continue
        try:
            payload: Any = json.loads(message.content)
        except json.JSONDecodeError:
            continue
        arguments = arguments_by_call.get(message.tool_call_id or "", {})
        repo_id = arguments.get("repo_id")
        resolved_id = repo_id if _is_real_int(repo_id) else None
        name = message.name or ""
        if name == "get_repository_profile" and resolved_id is not None:
            if isinstance(payload, dict):
                evidence.profiles[resolved_id] = payload
        elif name == "query_repo_metrics" and resolved_id is not None:
            if isinstance(payload, list):
                evidence.metrics[resolved_id] = [
                    point for point in payload if isinstance(point, dict)
                ]
        elif name == "get_repository_content" and resolved_id is not None:
            if isinstance(payload, dict):
                evidence.contents[resolved_id] = payload
        elif name == "search_prior_reports" and isinstance(payload, list):
            evidence.prior_reports = [item for item in payload if isinstance(item, dict)]
    return evidence


def _metric_citation(repo_id: int, points: Sequence[dict[str, Any]]) -> CitationRef:
    """Build a traceable citation to the latest snapshot of a repository."""
    latest = points[-1].get("snapshot_date") if points else None
    suffix = latest if isinstance(latest, str) else "unknown"
    return CitationRef(source_type="metric", source_ref=f"repo_snapshots:{repo_id}:{suffix}")


def _readme_citation(repo_id: int) -> CitationRef:
    """Build a traceable citation to the cached README of a repository."""
    return CitationRef(
        source_type="readme",
        source_ref=f"repository_contents:{repo_id}:readme",
    )


def _prior_report_citation(report_id: int) -> CitationRef:
    """Build a traceable citation to one prior published report."""
    return CitationRef(source_type="prior_report", source_ref=f"reports:{report_id}")


def _repository_label(evidence: _Evidence, repo_id: int) -> str:
    """Return a human-readable repository label, falling back to the raw id."""
    profile = evidence.profiles.get(repo_id)
    if profile is None:
        return f"repo#{repo_id}"
    full_name = profile.get("full_name")
    return full_name if isinstance(full_name, str) and full_name else f"repo#{repo_id}"


def compose_sections(evidence: _Evidence) -> list[dict[str, Any]]:
    """Turn the research snapshot into one deterministic draft per report section.

    Every section is derived from recorded observations only. README text is never copied
    into the draft: it stays marked as untrusted input and is only acknowledged as a source.
    """
    repo_ids = evidence.repo_ids()
    deltas = {repo_id: _net_star_delta(evidence.metrics.get(repo_id, [])) for repo_id in repo_ids}
    ranked = sorted(repo_ids, key=lambda repo_id: (-deltas[repo_id], repo_id))
    total_delta = sum(deltas.values())
    growing = [repo_id for repo_id in ranked if deltas[repo_id] > 0]
    stagnant = [repo_id for repo_id in ranked if deltas[repo_id] <= 0]

    metric_citations = [
        _metric_citation(repo_id, evidence.metrics[repo_id])
        for repo_id in repo_ids
        if evidence.metrics.get(repo_id)
    ]
    readme_citations = [
        _readme_citation(repo_id) for repo_id in repo_ids if repo_id in evidence.contents
    ]
    prior_citations = [
        _prior_report_citation(report_id)
        for report in evidence.prior_reports
        if _is_real_int(report.get("report_id"))
        for report_id in [report["report_id"]]
    ]

    summary_lines = [
        f"本期研究窗口覆盖 {len(repo_ids)} 个候选仓库，合计净增 {total_delta} stars。",
    ]
    if growing:
        top = "、".join(_repository_label(evidence, repo_id) for repo_id in growing[:2])
        summary_lines.append(f"增长最明显的仓库为 {top}。")
    if stagnant:
        flat = "、".join(_repository_label(evidence, repo_id) for repo_id in stagnant[:2])
        summary_lines.append(f"{flat} 在本期变化平稳，未观察到明显热度上升。")

    trend_lines = []
    for repo_id in repo_ids:
        points = evidence.metrics.get(repo_id, [])
        first = _int_field(points[0], "stars") if points else 0
        last = _int_field(points[-1], "stars") if points else 0
        trend_lines.append(
            f"- {_repository_label(evidence, repo_id)}：stars 由 {first} 变化到 {last}，"
            f"净增 {deltas[repo_id]}，共 {len(points)} 个观测点。"
        )
    if not trend_lines:
        trend_lines.append("- 本期没有可用的时间序列观测点。")

    highlight_lines = [
        f"- {_repository_label(evidence, repo_id)}：净增 {deltas[repo_id]} stars。"
        for repo_id in growing[:3]
    ] or ["- 本期没有观察到净增为正的仓库。"]

    risk_lines = []
    for repo_id in stagnant[:3]:
        points = evidence.metrics.get(repo_id, [])
        if not points:
            risk_lines.append(f"- {_repository_label(evidence, repo_id)}：缺少时间序列观测。")
        else:
            risk_lines.append(
                f"- {_repository_label(evidence, repo_id)}：净增 {deltas[repo_id]}，"
                "热度未提升或出现回落。"
            )
    if repo_ids and len(evidence.contents) < len(repo_ids):
        missing = [repo_id for repo_id in repo_ids if repo_id not in evidence.contents]
        labels = "、".join(_repository_label(evidence, repo_id) for repo_id in missing[:3])
        risk_lines.append(f"- {labels}：本期没有缓存到 README，结论仅基于指标数据。")
    if not risk_lines:
        risk_lines.append("- 本期未发现需要特别提示的风险项。")

    recommendation_lines = []
    if growing:
        recommendation_lines.append(
            "建议优先跟进 {} 的近期动态，并核对是否有新的发布或生态变化。".format(
                "、".join(_repository_label(evidence, repo_id) for repo_id in growing[:2])
            )
        )
    if stagnant:
        recommendation_lines.append("建议将变化平稳的仓库降为观察项，待出现增长信号后重新评估。")
    recommendation_lines.append(
        "上述结论仅基于已入库的指标与缓存内容，正式发布前应由人工复核关键数字。"
    )

    appendix_lines = [
        "数据来源：",
        f"- 指标观测点：{len(metric_citations)} 组（repo_snapshots）。",
        f"- 缓存 README：{len(evidence.contents)} 个（repository_contents，标记为不可信输入）。",
        f"- 历史报告：{len(evidence.prior_reports)} 篇（reports）。",
        "仓库 README 属于外部不可信文本，仅用于交叉核对，不直接引用其原文。",
    ]

    # Each section cites exactly the observations its prose is derived from. The metric
    # series is the shared spine of every section, so a section never ends up with an empty
    # citation list just because the topic has no prior reports and no cached README yet.
    raw_sections: dict[str, tuple[list[str], list[CitationRef]]] = {
        "summary": (summary_lines, metric_citations),
        "trend": (trend_lines, metric_citations),
        "highlights": (highlight_lines, metric_citations),
        "risks": (risk_lines, metric_citations + readme_citations),
        "recommendation": (recommendation_lines, metric_citations + prior_citations),
        "appendix": (appendix_lines, metric_citations + readme_citations + prior_citations),
    }
    return [
        {
            "section_key": section_key,
            "content_markdown": "\n".join(lines),
            "citations": [citation.model_dump() for citation in citations],
        }
        for section_key in REPORT_SECTION_KEYS
        for lines, citations in [raw_sections[section_key]]
    ]


class OfflineModelClient:
    """Deterministic, network-free implementation of the model client contract."""

    def __init__(
        self,
        model: str = DEFAULT_OFFLINE_MODEL,
        *,
        content_repo_limit: int = DEFAULT_CONTENT_REPO_LIMIT,
        prior_report_limit: int = DEFAULT_PRIOR_REPORT_LIMIT,
    ) -> None:
        self._model = model
        self._content_repo_limit = max(1, content_repo_limit)
        self._prior_report_limit = max(1, min(prior_report_limit, MAX_PRIOR_REPORT_LIMIT))

    @property
    def provider(self) -> str:
        """Return the offline provider identifier."""
        return "offline"

    @property
    def model(self) -> str:
        """Return the stub model identifier."""
        return self._model

    def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema],
    ) -> ModelTurn:
        """Advance one deterministic research step, or draft the report when no tools are given."""
        if tools:
            return self._plan_turn(messages)
        return self._write_turn(messages)

    def _turn(self, calls: Sequence[ModelToolCall] = (), text: str = "") -> ModelTurn:
        """Build one stub turn with zero cost, because the stub never calls a paid model."""
        return ModelTurn(text=text, tool_calls=tuple(calls), model=self._model)

    def _call(self, name: str, arguments: dict[str, Any]) -> ModelToolCall:
        """Build a deterministic tool call whose id encodes the tool and target."""
        repo_id = arguments.get("repo_id")
        suffix = f"-r{repo_id}" if _is_real_int(repo_id) else ""
        return ModelToolCall(call_id=f"{name}{suffix}", name=name, arguments=arguments)

    def _plan_turn(self, messages: Sequence[ChatMessage]) -> ModelTurn:
        """Return the next batch of research tool calls for the current evidence state."""
        candidates = extract_candidate_ids(messages)
        requested = requested_tool_calls(messages)
        if not candidates:
            return self._turn(text="没有可研究的候选仓库，结束证据收集。")

        pending = [
            repo_id
            for repo_id in candidates
            if ("get_repository_profile", repo_id) not in requested
        ]
        if pending:
            calls = [
                self._call("get_repository_profile", {"repo_id": repo_id}) for repo_id in pending
            ]
            return self._turn(calls)

        window = extract_window(messages)
        if window is None:
            return self._turn(text="缺少研究窗口，结束证据收集。")
        start, end = window

        pending = [
            repo_id for repo_id in candidates if ("query_repo_metrics", repo_id) not in requested
        ]
        if pending:
            calls = [
                self._call(
                    "query_repo_metrics",
                    {
                        "repo_id": repo_id,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "granularity": "day",
                    },
                )
                for repo_id in pending
            ]
            return self._turn(calls)

        topic_id = extract_topic_id(messages)
        if topic_id is not None and ("search_prior_reports", None) not in requested:
            return self._turn(
                [
                    self._call(
                        "search_prior_reports",
                        {
                            "topic_id": topic_id,
                            "before": end.isoformat(),
                            "limit": self._prior_report_limit,
                        },
                    )
                ]
            )

        pending = [
            repo_id
            for repo_id in candidates[: self._content_repo_limit]
            if ("get_repository_content", repo_id) not in requested
        ]
        if pending:
            calls = [
                self._call("get_repository_content", {"repo_id": repo_id}) for repo_id in pending
            ]
            return self._turn(calls)

        return self._turn(text="证据收集完成。")

    def _write_turn(self, messages: Sequence[ChatMessage]) -> ModelTurn:
        """Return the draft sections as a JSON document for the runtime to parse."""
        payload = {"sections": compose_sections(_collect_evidence(messages))}
        return self._turn(text=json.dumps(payload, ensure_ascii=False, sort_keys=True))
