"""白名单 Agent 工具的 Pydantic 运行时契约。

给人读的契约放在 `docs/agent-tool-contract.md`。这里的字段名、类型、上限与
错误码，必须与该文档以及 `INTERFACES.md` 逐字节保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

T = TypeVar("T")

TOOL_INVALID_ARGUMENT = "TOOL_INVALID_ARGUMENT"
TOOL_ACCESS_DENIED = "TOOL_ACCESS_DENIED"
TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
TOOL_TIMEOUT = "TOOL_TIMEOUT"
TOOL_RATE_LIMITED = "TOOL_RATE_LIMITED"
TOOL_BUDGET_EXCEEDED = "TOOL_BUDGET_EXCEEDED"
TOOL_INTERNAL_ERROR = "TOOL_INTERNAL_ERROR"

STANDARD_ERROR_CODES = (
    TOOL_INVALID_ARGUMENT,
    TOOL_ACCESS_DENIED,
    TOOL_NOT_FOUND,
    TOOL_TIMEOUT,
    TOOL_RATE_LIMITED,
    TOOL_BUDGET_EXCEEDED,
    TOOL_INTERNAL_ERROR,
)

RETRYABLE_ERROR_CODES = frozenset({TOOL_TIMEOUT, TOOL_RATE_LIMITED})

MAX_METRIC_RANGE_DAYS = 366
MIN_CONTENT_CHARS = 500
MAX_CONTENT_CHARS = 12000
DEFAULT_CONTENT_CHARS = 6000
MAX_SECTION_CHARS = 20000
MIN_SECTION_DIGEST_CHARS = 8
MAX_SECTION_DIGEST_CHARS = 64
MAX_CITATIONS = 50
MIN_PRIOR_REPORT_LIMIT = 1
MAX_PRIOR_REPORT_LIMIT = 20

#: 「趋势追踪」报告的章节键名，保持既有六节结构不变。
TREND_SECTION_KEYS = (
    "summary",
    "trend",
    "highlights",
    "risks",
    "recommendation",
    "appendix",
)

#: 「热门工具追踪」报告的章节键名：核心是逐仓库盘点核心功能（`tools`），而不是
#: star 涨跌的走向判断，因此用「能力洞察」与「采纳建议」取代趋势类的「趋势」与「风险」。
HOT_TOOL_SECTION_KEYS = (
    "overview",
    "tools",
    "capabilities",
    "adoption",
    "appendix",
)

#: 报告类型 -> 该类型要求起草的章节，顺序即报告渲染顺序。
SECTION_KEYS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "trend": TREND_SECTION_KEYS,
    "hot_tool": HOT_TOOL_SECTION_KEYS,
}

#: 所有已知章节键名（跨类型并集，去重）。`SaveReportSectionInput` 用它在 Pydantic 层
#: 挡住明显的垃圾值；「是否属于当前报告类型」这一更窄的判断由保存工具与图各自执行。
ALL_SECTION_KEYS: tuple[str, ...] = tuple(
    dict.fromkeys((*TREND_SECTION_KEYS, *HOT_TOOL_SECTION_KEYS))
)


def section_keys_for(report_type: str) -> tuple[str, ...]:
    """返回某报告类型要求的章节键名；未知类型回退到趋势类型。

    回退到趋势类型是保守默认：`report_type` 受数据库 CHECK 约束，正常不会出现未知值，
    这里回退只是让「读到一行无法识别的类型」不至于让报告生成直接崩掉。
    """
    return SECTION_KEYS_BY_TYPE.get(report_type, TREND_SECTION_KEYS)


#: 既有代码里直接引用六个章节的地方，沿用趋势章节键名（默认行为）。
REPORT_SECTION_KEYS = TREND_SECTION_KEYS


#: 白名单证据工具名，与 `graph.py` 的验证节点、`offline.py` 的桩模型共享。
PROFILE_TOOL = "get_repository_profile"
METRICS_TOOL = "query_repo_metrics"
CONTENT_TOOL = "get_repository_content"
PRIOR_TOOL = "search_prior_reports"


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    """某类报告要通过证据校验所需收集的证据规格。

    - `per_candidate_tools`：仓库级证据，**每个**候选仓库都必须有成功观测；
    - `at_least_one_tools`：仓库级证据，全体候选**至少一份**成功观测；
    - `at_least_one_topic_tools`：主题级证据（不绑定单个仓库），至少一次成功观测。

    趋势类沿用既有语义（每候选画像 + 指标，至少一份正文 + 一次历史基线）；热门工具
    追踪以「逐仓库盘点核心功能」为主线，正文（README）是每候选的硬要求，指标退为
    「至少一份」的佐证。两者证据下限都是 `2 × 候选数 + 2`，但语义不同。
    """

    per_candidate_tools: tuple[str, ...]
    at_least_one_tools: tuple[str, ...]
    at_least_one_topic_tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResearchBudgetSpec:
    """某类报告研究运行的预算加值。

    `extra_*` 是在 `Settings` 基础预算（`agent_max_*`）之上的加值，而不是替换：
    `trend` 加值为零，保持既有预算不变；新报告类型若要更多轮次/调用，只在这里加，
    不动 `Settings` 默认值，也不改 `agent_configs` 存储值。
    """

    extra_loops: int = 0
    extra_model_calls: int = 0
    extra_tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class ResearchSpec:
    """一类报告的研究规格：证据要求 + 预算加值。"""

    evidence: EvidenceRequirement
    budget: ResearchBudgetSpec


#: 报告类型 -> 研究规格。未来新增报告类型只需在这里加一个条目，验证器与预算门槛
#: 会自动跟随，无需改 `graph.py` / `runtime.py` 里的分派逻辑。
RESEARCH_SPECS_BY_TYPE: dict[str, ResearchSpec] = {
    "trend": ResearchSpec(
        evidence=EvidenceRequirement(
            per_candidate_tools=(PROFILE_TOOL, METRICS_TOOL),
            at_least_one_tools=(CONTENT_TOOL,),
            at_least_one_topic_tools=(PRIOR_TOOL,),
        ),
        budget=ResearchBudgetSpec(),
    ),
    "hot_tool": ResearchSpec(
        evidence=EvidenceRequirement(
            per_candidate_tools=(PROFILE_TOOL, CONTENT_TOOL),
            at_least_one_tools=(METRICS_TOOL,),
            at_least_one_topic_tools=(PRIOR_TOOL,),
        ),
        # 逐仓库盘点核心功能要比趋势类多读 `n - 1` 份正文，分布在更多轮次里，也更容易
        # 在 nudge 引导补证据时撞上轮次上限。多给两轮循环与两次模型调用，避免像真实模型
        # 那样证据没凑齐就停。
        budget=ResearchBudgetSpec(extra_loops=2, extra_model_calls=2),
    ),
}


def research_spec_for(report_type: str) -> ResearchSpec:
    """返回某报告类型的研究规格；未知类型回退到趋势类型。

    回退原因与 `section_keys_for` 一致：`report_type` 受数据库 CHECK 约束，正常不会
    出现未知值，这里只是让读到无法识别的类型不至于让研究运行直接崩掉。
    """
    return RESEARCH_SPECS_BY_TYPE.get(report_type, RESEARCH_SPECS_BY_TYPE["trend"])


def evidence_requirement_for(report_type: str) -> EvidenceRequirement:
    """返回某报告类型的证据规格。"""
    return research_spec_for(report_type).evidence


class StrictModel(BaseModel):
    """拒绝未知字段、并拒绝类型强转的基类模型。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class ToolError(StrictModel):
    """稳定、且对模型安全的错误载荷。"""

    code: str
    message: str
    retryable: bool


class ToolMeta(StrictModel):
    """每次工具结果都必须携带的调用元数据。"""

    tool_name: str
    latency_ms: int = Field(ge=0)
    truncated: bool
    result_count: int = Field(ge=0)


class ToolResult(StrictModel, Generic[T]):
    """每次工具调用都返回的统一结果信封。"""

    ok: bool
    data: T | None = None
    error: ToolError | None = None
    meta: ToolMeta


class QueryRepoMetricsInput(StrictModel):
    """`query_repo_metrics` 的入参。"""

    repo_id: int = Field(gt=0)
    start: datetime
    end: datetime
    granularity: Literal["day"] = "day"

    @model_validator(mode="after")
    def validate_window(self) -> QueryRepoMetricsInput:
        """在访问数据库之前，先拒绝起止颠倒或跨度过长的窗口。"""
        if self.start > self.end:
            raise ValueError("start must not be after end")
        if self.end - self.start > timedelta(days=MAX_METRIC_RANGE_DAYS):
            raise ValueError(f"range must not exceed {MAX_METRIC_RANGE_DAYS} days")
        return self


class MetricPoint(StrictModel):
    """仓库的单个每日指标点。"""

    snapshot_date: date
    stars: int
    forks: int
    open_issues: int | None = None
    stars_delta: int | None = None
    forks_delta: int | None = None


class GetRepositoryProfileInput(StrictModel):
    """`get_repository_profile` 的入参。"""

    repo_id: int = Field(gt=0)


class RepositoryProfile(StrictModel):
    """仓库主数据与最新计数。"""

    repo_id: int
    provider: str
    full_name: str
    html_url: str
    description: str | None = None
    language: str | None = None
    license: str | None = None
    topics: list[str]
    stars: int
    forks: int
    first_seen_at: datetime
    last_seen_at: datetime


class GetRepositoryContentInput(StrictModel):
    """`get_repository_content` 的入参。"""

    repo_id: int = Field(gt=0)
    content_type: Literal["readme"] = "readme"
    max_chars: int = Field(
        default=DEFAULT_CONTENT_CHARS,
        ge=MIN_CONTENT_CHARS,
        le=MAX_CONTENT_CHARS,
    )
    query_hint: str | None = Field(default=None, max_length=200)


class ContentSnippet(StrictModel):
    """已清洗、已截断、且始终标记为不可信的仓库内容。"""

    repo_id: int
    source_url: str
    content: str
    content_hash: str
    truncated: bool
    untrusted: Literal[True] = True


class SearchPriorReportsInput(StrictModel):
    """`search_prior_reports` 的入参。"""

    topic_id: int = Field(gt=0)
    repo_id: int | None = Field(default=None, gt=0)
    before: datetime
    limit: int = Field(
        default=10,
        ge=MIN_PRIOR_REPORT_LIMIT,
        le=MAX_PRIOR_REPORT_LIMIT,
    )
    query: str | None = Field(default=None, max_length=200)


class ReportSummary(StrictModel):
    """一份往期报告的紧凑视图。"""

    report_id: int
    title: str
    generated_at: datetime
    section_key: str
    summary: str
    source_refs: list[str]


class CitationRef(StrictModel):
    """挂在某个已保存章节上的一条证据引用。

    类型仍然严格，但容忍未知字段。引用是草稿信封的*内层*部分，
    而外层信封早已放宽为 `extra="ignore"`：如果这里继续严格，模型只要给某条
    引用加上 `"claim": "..."`，整份草稿仍会校验失败，
    那么失败率终究还是由外层信封说了算。
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    source_type: Literal["metric", "readme", "prior_report"]
    source_ref: str = Field(min_length=1, max_length=255)
    source_url: str | None = Field(default=None, max_length=512)


class SaveReportSectionInput(StrictModel):
    """`save_report_section` 的入参。"""

    section_key: str
    content_markdown: str = Field(min_length=1, max_length=MAX_SECTION_CHARS)
    citations: list[CitationRef] = Field(default_factory=list, max_length=MAX_CITATIONS)
    content_digest: str = Field(
        min_length=MIN_SECTION_DIGEST_CHARS,
        max_length=MAX_SECTION_DIGEST_CHARS,
    )

    @field_validator("section_key")
    @classmethod
    def validate_section_key(cls, value: str) -> str:
        """只允许写入已知章节键名。

        这里挡的是「明显不属于任何报告类型」的垃圾值；是否属于当前运行报告类型的那几个
        章节，由保存工具按 `ToolContext.section_keys` 再校验一次 —— 那需要读到运行上下文，
        而 Pydantic 层拿不到它。
        """
        if value not in ALL_SECTION_KEYS:
            raise ValueError(f"section_key must be one of: {', '.join(ALL_SECTION_KEYS)}")
        return value


class SectionRef(StrictModel):
    """指向某个已存报告草稿章节版本的引用。"""

    report_draft_id: int
    section_key: str
    version: int
    saved_at: datetime
