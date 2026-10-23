"""Pydantic runtime contract for the whitelisted Agent tools.

The human-readable contract lives in `docs/agent-tool-contract.md`. Field names,
types, limits, and error codes here must stay byte-for-byte consistent with that
document and with `INTERFACES.md`.
"""

from __future__ import annotations

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

REPORT_SECTION_KEYS = (
    "summary",
    "trend",
    "highlights",
    "risks",
    "recommendation",
    "appendix",
)


class StrictModel(BaseModel):
    """Base model that rejects unknown fields and refuses type coercion."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ToolError(StrictModel):
    """Stable, model-safe error payload."""

    code: str
    message: str
    retryable: bool


class ToolMeta(StrictModel):
    """Invocation metadata every tool result must carry."""

    tool_name: str
    latency_ms: int = Field(ge=0)
    truncated: bool
    result_count: int = Field(ge=0)


class ToolResult(StrictModel, Generic[T]):
    """Uniform result envelope returned by every tool invocation."""

    ok: bool
    data: T | None = None
    error: ToolError | None = None
    meta: ToolMeta


class QueryRepoMetricsInput(StrictModel):
    """Input for `query_repo_metrics`."""

    repo_id: int = Field(gt=0)
    start: datetime
    end: datetime
    granularity: Literal["day"] = "day"

    @model_validator(mode="after")
    def validate_window(self) -> QueryRepoMetricsInput:
        """Reject inverted or over-long windows before touching the database."""
        if self.start > self.end:
            raise ValueError("start must not be after end")
        if self.end - self.start > timedelta(days=MAX_METRIC_RANGE_DAYS):
            raise ValueError(f"range must not exceed {MAX_METRIC_RANGE_DAYS} days")
        return self


class MetricPoint(StrictModel):
    """One daily metric point for a repository."""

    snapshot_date: date
    stars: int
    forks: int
    open_issues: int | None = None
    stars_delta: int | None = None
    forks_delta: int | None = None


class GetRepositoryProfileInput(StrictModel):
    """Input for `get_repository_profile`."""

    repo_id: int = Field(gt=0)


class RepositoryProfile(StrictModel):
    """Repository master data and latest counters."""

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
    """Input for `get_repository_content`."""

    repo_id: int = Field(gt=0)
    content_type: Literal["readme"] = "readme"
    max_chars: int = Field(
        default=DEFAULT_CONTENT_CHARS,
        ge=MIN_CONTENT_CHARS,
        le=MAX_CONTENT_CHARS,
    )
    query_hint: str | None = Field(default=None, max_length=200)


class ContentSnippet(StrictModel):
    """Cleaned, truncated, and always-untrusted repository content."""

    repo_id: int
    source_url: str
    content: str
    content_hash: str
    truncated: bool
    untrusted: Literal[True] = True


class SearchPriorReportsInput(StrictModel):
    """Input for `search_prior_reports`."""

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
    """Compact view of one prior report."""

    report_id: int
    title: str
    generated_at: datetime
    section_key: str
    summary: str
    source_refs: list[str]


class CitationRef(StrictModel):
    """One evidence reference attached to a saved section.

    Types are still strict, but unknown fields are tolerated. A citation is the *inner* part of a
    draft envelope, and the envelope was already relaxed to `extra="ignore"`: leaving this one
    strict would mean a model that adds `"claim": "..."` to a citation still fails the entire
    draft, so the failure rate would follow the envelope after all.
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    source_type: Literal["metric", "readme", "prior_report"]
    source_ref: str = Field(min_length=1, max_length=255)
    source_url: str | None = Field(default=None, max_length=512)


class SaveReportSectionInput(StrictModel):
    """Input for `save_report_section`."""

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
        """Only report template sections may be written."""
        if value not in REPORT_SECTION_KEYS:
            raise ValueError(f"section_key must be one of: {', '.join(REPORT_SECTION_KEYS)}")
        return value


class SectionRef(StrictModel):
    """Reference to a stored report-draft section version."""

    report_draft_id: int
    section_key: str
    version: int
    saved_at: datetime
