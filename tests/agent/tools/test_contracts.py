"""守护白名单 Agent 工具 schema 契约的用例。"""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from open_source_intelligence.agent.tools.contracts import (
    MAX_METRIC_RANGE_DAYS,
    REPORT_SECTION_KEYS,
    STANDARD_ERROR_CODES,
    ContentSnippet,
    GetRepositoryContentInput,
    MetricPoint,
    QueryRepoMetricsInput,
    SaveReportSectionInput,
    SearchPriorReportsInput,
    ToolError,
    ToolMeta,
    ToolResult,
)

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def test_tool_result_envelope_round_trips() -> None:
    """证明每个工具结果都带 ok、data、error 与 meta。"""
    result = ToolResult[list[MetricPoint]](
        ok=True,
        data=[MetricPoint(snapshot_date=date(2026, 10, 12), stars=5, forks=1)],
        error=None,
        meta=ToolMeta(
            tool_name="query_repo_metrics",
            latency_ms=3,
            truncated=False,
            result_count=1,
        ),
    )
    assert result.ok is True
    assert result.error is None
    assert result.meta.result_count == 1
    assert result.data is not None and result.data[0].stars == 5


def test_tool_error_rejects_unknown_fields() -> None:
    """证明信封会拒绝不属于契约的字段。"""
    with pytest.raises(ValidationError):
        ToolError(code="X", message="m", retryable=False, unexpected=1)  # type: ignore[call-arg]


def test_query_metrics_rejects_inverted_window() -> None:
    """证明 start 不得晚于 end。"""
    with pytest.raises(ValidationError):
        QueryRepoMetricsInput(repo_id=1, start=NOW, end=datetime(2026, 10, 1, tzinfo=UTC))


def test_query_metrics_rejects_over_long_window() -> None:
    """证明超过上限的时间窗会被拒绝，而不是被截断。"""
    with pytest.raises(ValidationError):
        QueryRepoMetricsInput(
            repo_id=1,
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2025, 1, 1, tzinfo=UTC).replace(year=2026, month=3),
        )
    allowed = QueryRepoMetricsInput(
        repo_id=1,
        start=NOW,
        end=NOW.replace(year=NOW.year + 1),
    )
    assert (allowed.end - allowed.start).days == 365
    assert MAX_METRIC_RANGE_DAYS == 366


def test_inputs_reject_unknown_fields() -> None:
    """证明未知参数绝不能走到查询里。"""
    with pytest.raises(ValidationError):
        QueryRepoMetricsInput(repo_id=1, start=NOW, end=NOW, repo_ids=[1, 2])  # type: ignore[call-arg]


def test_inputs_reject_wrong_types() -> None:
    """证明严格校验会拒绝「能强转但类型不对」的输入。"""
    with pytest.raises(ValidationError):
        QueryRepoMetricsInput(repo_id="1", start=NOW, end=NOW)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        QueryRepoMetricsInput(repo_id=0, start=NOW, end=NOW)


def test_content_char_bounds() -> None:
    """证明 max_chars 限定在 500–12000，默认 6000。"""
    assert GetRepositoryContentInput(repo_id=1).max_chars == 6000
    with pytest.raises(ValidationError):
        GetRepositoryContentInput(repo_id=1, max_chars=499)
    with pytest.raises(ValidationError):
        GetRepositoryContentInput(repo_id=1, max_chars=12001)


def test_content_snippet_must_be_untrusted() -> None:
    """证明外部内容不能被当作可信内容返回。"""
    with pytest.raises(ValidationError):
        ContentSnippet(
            repo_id=1,
            source_url="https://example.com",
            content="hello",
            content_hash="abc",
            truncated=False,
            untrusted=False,  # type: ignore[arg-type]
        )


def test_section_key_whitelist() -> None:
    """证明只允许保存报告模板里的章节。"""
    assert "summary" in REPORT_SECTION_KEYS
    with pytest.raises(ValidationError):
        SaveReportSectionInput(
            section_key="free_form",
            content_markdown="text",
            content_digest="0123456789ab",
        )


def test_section_content_and_digest_bounds() -> None:
    """证明正文与摘要的长度在契约边界上就被强制执行。"""
    with pytest.raises(ValidationError):
        SaveReportSectionInput(
            section_key="summary",
            content_markdown="x" * 20001,
            content_digest="0123456789ab",
        )
    with pytest.raises(ValidationError):
        SaveReportSectionInput(
            section_key="summary",
            content_markdown="text",
            content_digest="short",
        )
    accepted = SaveReportSectionInput(
        section_key="summary",
        content_markdown="text",
        content_digest="0123456789abcdef",
        citations=[{"source_type": "metric", "source_ref": "repo:1#stars"}],
    )
    assert accepted.citations[0].source_ref == "repo:1#stars"


def test_citation_source_type_is_validated() -> None:
    """证明引用必须使用文档里列出的来源类型。"""
    with pytest.raises(ValidationError):
        SaveReportSectionInput(
            section_key="summary",
            content_markdown="text",
            content_digest="0123456789ab",
            citations=[{"source_type": "web", "source_ref": "x"}],
        )


def test_prior_report_limit_bounds() -> None:
    """证明 limit 限定在 1–20。"""
    assert SearchPriorReportsInput(topic_id=1, before=NOW).limit == 10
    with pytest.raises(ValidationError):
        SearchPriorReportsInput(topic_id=1, before=NOW, limit=0)
    with pytest.raises(ValidationError):
        SearchPriorReportsInput(topic_id=1, before=NOW, limit=21)


def test_standard_error_codes_are_frozen() -> None:
    """证明文档化的错误码集合不得悄悄变化。"""
    assert set(STANDARD_ERROR_CODES) == {
        "TOOL_INVALID_ARGUMENT",
        "TOOL_ACCESS_DENIED",
        "TOOL_NOT_FOUND",
        "TOOL_TIMEOUT",
        "TOOL_RATE_LIMITED",
        "TOOL_BUDGET_EXCEEDED",
        "TOOL_INTERNAL_ERROR",
    }
