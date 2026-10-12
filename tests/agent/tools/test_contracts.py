"""Schema contract tests for the whitelisted Agent tools."""

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
    """Every tool result carries ok, data, error, and meta."""
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
    """The envelope refuses fields that are not part of the contract."""
    with pytest.raises(ValidationError):
        ToolError(code="X", message="m", retryable=False, unexpected=1)  # type: ignore[call-arg]


def test_query_metrics_rejects_inverted_window() -> None:
    """start must not be after end."""
    with pytest.raises(ValidationError):
        QueryRepoMetricsInput(repo_id=1, start=NOW, end=datetime(2026, 10, 1, tzinfo=UTC))


def test_query_metrics_rejects_over_long_window() -> None:
    """A window longer than the maximum is refused, not truncated."""
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
    """Unknown parameters must never reach a query."""
    with pytest.raises(ValidationError):
        QueryRepoMetricsInput(repo_id=1, start=NOW, end=NOW, repo_ids=[1, 2])  # type: ignore[call-arg]


def test_inputs_reject_wrong_types() -> None:
    """Strict validation refuses coercible but wrong types."""
    with pytest.raises(ValidationError):
        QueryRepoMetricsInput(repo_id="1", start=NOW, end=NOW)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        QueryRepoMetricsInput(repo_id=0, start=NOW, end=NOW)


def test_content_char_bounds() -> None:
    """max_chars is limited to 500-12000 and defaults to 6000."""
    assert GetRepositoryContentInput(repo_id=1).max_chars == 6000
    with pytest.raises(ValidationError):
        GetRepositoryContentInput(repo_id=1, max_chars=499)
    with pytest.raises(ValidationError):
        GetRepositoryContentInput(repo_id=1, max_chars=12001)


def test_content_snippet_must_be_untrusted() -> None:
    """External content cannot be returned as trusted."""
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
    """Only report template sections may be saved."""
    assert "summary" in REPORT_SECTION_KEYS
    with pytest.raises(ValidationError):
        SaveReportSectionInput(
            section_key="free_form",
            content_markdown="text",
            content_digest="0123456789ab",
        )


def test_section_content_and_digest_bounds() -> None:
    """Content and digest lengths are enforced at the contract boundary."""
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
    """Citations must use the documented source types."""
    with pytest.raises(ValidationError):
        SaveReportSectionInput(
            section_key="summary",
            content_markdown="text",
            content_digest="0123456789ab",
            citations=[{"source_type": "web", "source_ref": "x"}],
        )


def test_prior_report_limit_bounds() -> None:
    """limit is bounded to 1-20."""
    assert SearchPriorReportsInput(topic_id=1, before=NOW).limit == 10
    with pytest.raises(ValidationError):
        SearchPriorReportsInput(topic_id=1, before=NOW, limit=0)
    with pytest.raises(ValidationError):
        SearchPriorReportsInput(topic_id=1, before=NOW, limit=21)


def test_standard_error_codes_are_frozen() -> None:
    """The documented error code set must not silently change."""
    assert set(STANDARD_ERROR_CODES) == {
        "TOOL_INVALID_ARGUMENT",
        "TOOL_ACCESS_DENIED",
        "TOOL_NOT_FOUND",
        "TOOL_TIMEOUT",
        "TOOL_RATE_LIMITED",
        "TOOL_BUDGET_EXCEEDED",
        "TOOL_INTERNAL_ERROR",
    }
