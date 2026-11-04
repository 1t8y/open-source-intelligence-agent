"""供本地运行与评估基线使用的确定性离线模型客户端。

这个桩不做任何网络调用。它读取的 prompt 上下文与远程模型收到的完全
相同，然后每轮推进一个固定的研究步骤：

    profile -> metrics -> prior reports -> content -> final

由于策略是对话的纯函数，同样的输入跑两遍会得到逐字节相同的回复。这既
让它能当评估基线，也让整张图可以在没有出网访问的机器上跑完。
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
    is_tool_error_payload,
)

DEFAULT_OFFLINE_MODEL = "offline-deterministic-v1"

#: 深读 README 是最贵的一步，所以桩只把它花在排名靠前的候选上。
DEFAULT_CONTENT_REPO_LIMIT = 2
DEFAULT_PRIOR_REPORT_LIMIT = 5


@cache
def _block_pattern(tag: str) -> re.Pattern[str]:
    """返回某个上下文标签对应的、带缓存的非贪婪匹配器。"""
    return re.compile(rf"<{tag}>(.*?)</{tag}>", re.DOTALL)


def _extract_tag(messages: Sequence[ChatMessage], tag: str) -> str | None:
    """返回第一个带指定标签的用户上下文块。

    只检查 user 消息。系统提示词用占位示例说明块语法，如果连它也扫，
    那些占位符就会冒充本次运行的真实上下文。
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
    """判断一个 JSON 值是否为整数而非布尔值。"""
    return isinstance(value, int) and not isinstance(value, bool)


def extract_candidate_ids(messages: Sequence[ChatMessage]) -> tuple[int, ...]:
    """返回 prompt 上下文里声明的候选仓库 id。"""
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
    """返回 prompt 上下文里声明的主题 id。"""
    raw = _extract_tag(messages, TOPIC_TAG)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def extract_window(messages: Sequence[ChatMessage]) -> tuple[datetime, datetime] | None:
    """返回 prompt 上下文里声明的研究窗口。"""
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
    """返回助手已经请求过的所有 `(tool_name, repo_id)` 组合。"""
    requested: set[tuple[str, int | None]] = set()
    for message in messages:
        if message.role != "assistant":
            continue
        for call in message.tool_calls:
            repo_id = call.arguments.get("repo_id")
            requested.add((call.name, repo_id if _is_real_int(repo_id) else None))
    return requested


def _arguments_by_call_id(messages: Sequence[ChatMessage]) -> dict[str, dict[str, Any]]:
    """把每个工具调用 id 映射回助手当时给出的参数。"""
    mapping: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.role != "assistant":
            continue
        for call in message.tool_calls:
            mapping[call.call_id] = call.arguments
    return mapping


@dataclass(slots=True)
class _Evidence:
    """把工具观测重新拼装成的可比较快照。"""

    profiles: dict[int, dict[str, Any]] = field(default_factory=dict)
    metrics: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    contents: dict[int, dict[str, Any]] = field(default_factory=dict)
    prior_reports: list[dict[str, Any]] = field(default_factory=list)

    def repo_ids(self) -> list[int]:
        """返回所有观测类型中出现过的仓库 id。"""
        seen = set(self.profiles) | set(self.metrics) | set(self.contents)
        return sorted(seen)


def _int_field(payload: dict[str, Any], key: str) -> int:
    """防御式读取一个整数字段，其它类型一律按 0 处理。"""
    value = payload.get(key)
    if not _is_real_int(value):
        return 0
    return value


def _net_star_delta(points: Sequence[dict[str, Any]]) -> int:
    """返回有序指标序列中「末点减首点」的 stars 变化量。"""
    if len(points) < 2:
        return 0
    return _int_field(points[-1], "stars") - _int_field(points[0], "stars")


@dataclass(frozen=True, slots=True)
class _Trend:
    """对单个仓库指标序列的定性解读。

    报告的 `trend` 一节应当说清*正在发生什么* —— 主题在升温、降温还是
    走平 —— 而不是把其它小节已经列过的计数再念一遍。从每日增量里推出
    方向与加速度，才能把只有两点的「前后对比」变成读者可以据以行动的判断。
    """

    direction: str  # "rising" / "steady" / "declining" / "unknown"
    acceleration: str  # "accelerating" / "decelerating" / "steady" / "unknown"
    net_delta: int
    point_count: int

    @property
    def label(self) -> str:
        """报告正文用的简短中文判读。"""
        direction_label = {
            "rising": "升温",
            "steady": "平稳",
            "declining": "回落",
            "unknown": "数据不足",
        }[self.direction]
        # 走平的序列没有值得一提的加速度；在「平稳」后面再补一句「增速平稳」
        # 是同义反复。只有有方向的序列才追加加速度限定词。
        if self.direction in {"steady", "unknown"} or self.acceleration in {"steady", "unknown"}:
            return direction_label
        acceleration_label = {
            "accelerating": "增速加快",
            "decelerating": "增速放缓",
        }[self.acceleration]
        return f"{direction_label}（{acceleration_label}）"


def _daily_deltas(points: Sequence[dict[str, Any]]) -> list[int]:
    """返回每日 stars 增量，优先用存好的增量而不是相邻两点相减。

    `repo_snapshots.stars_delta` 可能为 null（早期的种子数据只存了绝对值），
    此时退回到相邻两个 `stars` 值相减。两者都是整数，调用方只关心符号与量级。
    """
    deltas: list[int] = []
    for index, point in enumerate(points):
        stored = point.get("stars_delta")
        if _is_real_int(stored):
            deltas.append(stored)
        elif index > 0:
            deltas.append(_int_field(point, "stars") - _int_field(points[index - 1], "stars"))
    return deltas


def _analyze_trend(points: Sequence[dict[str, Any]]) -> _Trend:
    """把一个指标序列归类成方向与加速度。"""
    net = _net_star_delta(points)
    count = len(points)
    if count < 2:
        return _Trend("unknown", "unknown", net, count)

    deltas = _daily_deltas(points)
    if not deltas:
        return _Trend("unknown", "unknown", net, count)

    total = sum(deltas)
    if total > 0:
        direction = "rising"
    elif total < 0:
        direction = "declining"
    else:
        direction = "steady"

    # 加速度比较的是序列后半段与前半段，因此一个仍在增长但正在放缓的序列
    # 会报「增速放缓」，而不是笼统的「升温」。只有两个点时只有一个增量，
    # 加速度无从判断。
    half = max(1, len(deltas) // 2)
    early = sum(deltas[:half])
    late = sum(deltas[half:])
    if len(deltas) < 2:
        acceleration = "unknown"
    elif late > early:
        acceleration = "accelerating"
    elif late < early:
        acceleration = "decelerating"
    else:
        acceleration = "steady"

    return _Trend(direction, acceleration, net, count)


def _collect_evidence(messages: Sequence[ChatMessage]) -> _Evidence:
    """从记录下来的工具观测重建研究快照。

    失败的观测在任何分支看到它之前就被跳过。失败与结果都是 JSON 对象，
    没有这一步，两者在这里无法区分，一个返回 `TOOL_NOT_FOUND` 的调用
    就会被归档成「读到了缓存的 README」—— Agent 随后会引用一份它从未
    收到的文档，报告生成则会在下游因为一条源行不存在的引用而失败。跳过
    这一步放在所有分支之前，而不是只加进那两个接受 dict 的分支，将来的
    新观测类型就无法靠漏写它把这个 bug 带回来。
    """
    evidence = _Evidence()
    arguments_by_call = _arguments_by_call_id(messages)
    for message in messages:
        if message.role != "tool" or not message.content:
            continue
        try:
            payload: Any = json.loads(message.content)
        except json.JSONDecodeError:
            continue
        if is_tool_error_payload(payload):
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
    """构造一条指向仓库最新快照的可溯源引用。"""
    latest = points[-1].get("snapshot_date") if points else None
    suffix = latest if isinstance(latest, str) else "unknown"
    return CitationRef(source_type="metric", source_ref=f"repo_snapshots:{repo_id}:{suffix}")


def _readme_citation(repo_id: int) -> CitationRef:
    """构造一条指向仓库缓存 README 的可溯源引用。"""
    return CitationRef(
        source_type="readme",
        source_ref=f"repository_contents:{repo_id}:readme",
    )


def _prior_report_citation(report_id: int) -> CitationRef:
    """构造一条指向某篇历史已发布报告的可溯源引用。"""
    return CitationRef(source_type="prior_report", source_ref=f"reports:{report_id}")


def _repository_label(evidence: _Evidence, repo_id: int) -> str:
    """返回便于人读的仓库标签，取不到时退回原始 id。"""
    profile = evidence.profiles.get(repo_id)
    if profile is None:
        return f"repo#{repo_id}"
    full_name = profile.get("full_name")
    return full_name if isinstance(full_name, str) and full_name else f"repo#{repo_id}"


def compose_sections(evidence: _Evidence) -> list[dict[str, Any]]:
    """把研究快照变成每个报告小节各一份的确定性草稿。

    每一节都只由记录下来的观测推导得出。README 原文绝不会被抄进草稿：
    它始终标记为不可信输入，最多只在来源里被提到。
    """
    repo_ids = evidence.repo_ids()
    trends = {repo_id: _analyze_trend(evidence.metrics.get(repo_id, [])) for repo_id in repo_ids}
    deltas = {repo_id: trends[repo_id].net_delta for repo_id in repo_ids}
    ranked = sorted(repo_ids, key=lambda repo_id: (-deltas[repo_id], repo_id))
    total_delta = sum(deltas.values())
    growing = [repo_id for repo_id in ranked if trends[repo_id].direction == "rising"]
    steady = [repo_id for repo_id in ranked if trends[repo_id].direction == "steady"]
    declining = [repo_id for repo_id in ranked if trends[repo_id].direction == "declining"]

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

    # 摘要给的是结论而不是普查：总变化量、升温/走平/回落三类的分布，以及
    # 变化有多集中。在这里点名前两名（以前的写法）只是换些词把 highlights
    # 一节又讲了一遍。只点名非零的分桶。「3 个升温、0 个平稳、0 个回落」
    # 会把开头那句的三分之一花在并不存在的类别上。
    buckets = (
        (len(growing), "升温"),
        (len(steady), "平稳"),
        (len(declining), "回落"),
    )
    split = [f"{count} 个{label}" for count, label in buckets if count]
    summary_lines = [
        f"本期 {len(repo_ids)} 个候选仓库合计净增 {total_delta} stars，"
        + ("、".join(split) if split else "无有效观测")
        + "。",
    ]
    if growing and total_delta > 0:
        leader = growing[0]
        share = deltas[leader] * 100.0 / total_delta
        summary_lines.append(
            f"增长高度集中：{_repository_label(evidence, leader)} 一家占本期净增的 {share:.0f}%。"
        )
    if declining:
        cooling = "、".join(_repository_label(evidence, repo_id) for repo_id in declining[:2])
        summary_lines.append(f"{cooling} 本期出现回落。")

    # trend 一节给的是*判断*，不是把计数复述一遍。它先给出主题层面的判读
    # （主题在升温还是降温），再逐个解释每个仓库的方向与加速度，读者不必
    # 自己从摘要的数字里再推一遍。
    trend_lines: list[str] = []
    if growing and not declining:
        trend_lines.append("本期主题整体处于**升温**阶段，多数候选仓库热度上行。")
    elif declining and not growing:
        trend_lines.append("本期主题整体处于**降温**阶段，多数候选仓库热度回落。")
    elif growing or declining:
        trend_lines.append("本期主题热度出现**分化**：部分仓库升温、部分回落，未形成单一方向。")
    else:
        trend_lines.append("本期主题整体**平稳**，各候选仓库热度未见明显方向性变化。")
    trend_lines.append("")
    # 按净变化排序而不是按仓库 id：读者想先看到决定了这一周的那一个，而按
    # id 排序会把一个 2,068 stars 的仓库排到 84,713 stars 的仓库前面。
    for repo_id in ranked:
        trend = trends[repo_id]
        points = evidence.metrics.get(repo_id, [])
        if trend.point_count < 2:
            trend_lines.append(
                f"- {_repository_label(evidence, repo_id)}：观测点不足，暂无法判断趋势。"
            )
        else:
            trend_lines.append(
                f"- {_repository_label(evidence, repo_id)}：{trend.label}，"
                f"净增 {trend.net_delta} stars（覆盖 {trend.point_count} 个快照）。"
            )
    if not repo_ids:
        trend_lines.append("- 本期没有可用的时间序列观测点。")

    # `highlights` 排的是 trend 一节描述过的同一批仓库，所以它必须补上
    # trend 没给的东西：每个变化者占主题总变化量的*份额*，这才说明是谁
    # 真正在扛着这个主题。逐字复述标签与增量（以前的写法）会让六个小节
    # 里的两节把同一句话说了两遍。
    highlight_lines = [
        f"- {_repository_label(evidence, repo_id)}：净增 {deltas[repo_id]} stars，"
        f"占本期合计净增的 {deltas[repo_id] * 100.0 / total_delta:.0f}%，"
        f"{trends[repo_id].label}。"
        for repo_id in ranked
        if deltas[repo_id] > 0 and total_delta > 0
    ][:3] or ["- 本期没有净增为正的仓库。"]

    risk_lines = []
    for repo_id in (declining + steady)[:3]:
        points = evidence.metrics.get(repo_id, [])
        if not points:
            risk_lines.append(f"- {_repository_label(evidence, repo_id)}：缺少时间序列观测。")
        else:
            risk_lines.append(
                f"- {_repository_label(evidence, repo_id)}：{trends[repo_id].label}，"
                "热度未提升或出现回落。"
            )
    if repo_ids and len(evidence.contents) < len(repo_ids):
        missing = [repo_id for repo_id in repo_ids if repo_id not in evidence.contents]
        labels = "、".join(_repository_label(evidence, repo_id) for repo_id in missing[:3])
        # 「未读取」是事实；「没有缓存到」是对内容存储的断言，而这段代码
        # 从不检查它 —— README 完全可能已缓存、只是没被读取，离线规划器
        # 超出内容预算之后正是这样。
        risk_lines.append(f"- {labels}：本期未读取 README，结论仅基于指标数据。")
    if not risk_lines:
        risk_lines.append("- 本期未发现需要特别提示的风险项。")

    recommendation_lines = []
    accelerating = [
        repo_id for repo_id in growing if trends[repo_id].acceleration == "accelerating"
    ]
    if accelerating:
        recommendation_lines.append(
            "建议优先跟进 {}，其增速正在加快，可能存在新的发布或生态催化。".format(
                "、".join(_repository_label(evidence, repo_id) for repo_id in accelerating[:2])
            )
        )
    elif growing:
        recommendation_lines.append(
            "建议关注 {} 的近期动态，并核对是否有新的发布或生态变化。".format(
                "、".join(_repository_label(evidence, repo_id) for repo_id in growing[:2])
            )
        )
    if declining:
        recommendation_lines.append(
            "建议核实 {} 回落的原因，判断是短暂波动还是方向性变化。".format(
                "、".join(_repository_label(evidence, repo_id) for repo_id in declining[:2])
            )
        )
    recommendation_lines.append(
        "上述结论仅基于已入库的指标与缓存内容，正式发布前应由人工复核关键数字。"
    )

    appendix_lines = [
        "数据来源：",
        f"- 指标序列：{len(metric_citations)} 组，"
        f"共覆盖 {sum(len(evidence.metrics.get(repo_id, [])) for repo_id in repo_ids)} 个快照"
        "（repo_snapshots）。",
        f"- 缓存 README：{len(evidence.contents)} 个（repository_contents，标记为不可信输入）。",
        f"- 历史报告：{len(evidence.prior_reports)} 篇（reports）。",
        "仓库 README 属于外部不可信文本，仅用于交叉核对，不直接引用其原文。",
    ]

    # 每一节只引用它那段文字真正推导自的观测。指标序列是所有小节共用的
    # 脊柱，因此一节不会仅仅因为主题还没有历史报告、也没有缓存 README
    # 就落得一个空的引用列表。
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
    """模型客户端契约的确定性、无网络实现。"""

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
        """返回离线供应商的标识。"""
        return "offline"

    @property
    def model(self) -> str:
        """返回桩模型标识。"""
        return self._model

    def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema],
    ) -> ModelTurn:
        """推进一个确定性的研究步骤；没有给出工具时改为起草报告。"""
        if tools:
            return self._plan_turn(messages)
        return self._write_turn(messages)

    def _turn(self, calls: Sequence[ModelToolCall] = (), text: str = "") -> ModelTurn:
        """构造一轮零成本的桩回复，因为桩从不调用付费模型。"""
        return ModelTurn(text=text, tool_calls=tuple(calls), model=self._model)

    def _call(self, name: str, arguments: dict[str, Any]) -> ModelToolCall:
        """构造一次确定性的工具调用，其 id 编码了工具与目标。"""
        repo_id = arguments.get("repo_id")
        suffix = f"-r{repo_id}" if _is_real_int(repo_id) else ""
        return ModelToolCall(call_id=f"{name}{suffix}", name=name, arguments=arguments)

    def _plan_turn(self, messages: Sequence[ChatMessage]) -> ModelTurn:
        """针对当前证据状态返回下一批研究工具调用。"""
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
        """把各节草稿作为 JSON 文档返回，交给运行时解析。"""
        payload = {"sections": compose_sections(_collect_evidence(messages))}
        return self._turn(text=json.dumps(payload, ensure_ascii=False, sort_keys=True))
