"""Whitelist registry, ACL/budget enforcement, and envelope handling for tools."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from open_source_intelligence.core.logging import redact_text
from open_source_intelligence.persistence.models.agent import ToolCall

from .context import ToolContext
from .contracts import (
    RETRYABLE_ERROR_CODES,
    TOOL_ACCESS_DENIED,
    TOOL_BUDGET_EXCEEDED,
    TOOL_INTERNAL_ERROR,
    TOOL_INVALID_ARGUMENT,
    GetRepositoryContentInput,
    GetRepositoryProfileInput,
    QueryRepoMetricsInput,
    SaveReportSectionInput,
    SearchPriorReportsInput,
    ToolError,
    ToolMeta,
    ToolResult,
)
from .errors import ToolExecutionError
from .get_repository_content import get_repository_content
from .get_repository_profile import get_repository_profile
from .outcome import ToolOutcome
from .query_repo_metrics import query_repo_metrics
from .save_report_section import save_report_section
from .search_prior_reports import search_prior_reports

WHITELIST = (
    "query_repo_metrics",
    "get_repository_profile",
    "get_repository_content",
    "search_prior_reports",
    "save_report_section",
)

ARGUMENT_PREVIEW_CHARS = 2000
ERROR_MESSAGE_CHARS = 500

ToolHandler = Callable[[Session, ToolContext, Any], ToolOutcome]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One registered tool: its schema, handler, and read-only classification.

    `description` is the human-readable line the model sees when the whitelist is exposed
    as JSON schemas; it must stay consistent with `docs/agent-tool-contract.md`.
    """

    name: str
    read_only: bool
    input_model: type[BaseModel]
    handler: ToolHandler
    description: str = ""


def build_default_registry() -> ToolRegistry:
    """Build the registry holding every whitelisted tool."""
    return ToolRegistry(
        (
            ToolSpec(
                name="query_repo_metrics",
                read_only=True,
                input_model=QueryRepoMetricsInput,
                handler=query_repo_metrics,
                description=(
                    "查询单个仓库在指定时间窗口内的每日指标时间序列"
                    "（stars、forks、open issues 及其增量）。只读。"
                ),
            ),
            ToolSpec(
                name="get_repository_profile",
                read_only=True,
                input_model=GetRepositoryProfileInput,
                handler=get_repository_profile,
                description=(
                    "获取单个仓库的主数据画像：来源、全名、链接、描述、语言、"
                    "许可证、主题标签与最新计数。只读。"
                ),
            ),
            ToolSpec(
                name="get_repository_content",
                read_only=True,
                input_model=GetRepositoryContentInput,
                handler=get_repository_content,
                description=(
                    "获取仓库缓存的 README 内容片段；内容会被清洗并按 max_chars 截断，"
                    "返回值始终标记为不可信外部文本。只读。"
                ),
            ),
            ToolSpec(
                name="search_prior_reports",
                read_only=True,
                input_model=SearchPriorReportsInput,
                handler=search_prior_reports,
                description=(
                    "查询授权主题下已发布的历史报告摘要，用于建立对比基线。只读，受主题 ACL 限制。"
                ),
            ),
            ToolSpec(
                name="save_report_section",
                read_only=False,
                input_model=SaveReportSectionInput,
                handler=save_report_section,
                description=(
                    "把一段草稿章节写入当前 Agent 运行的报告草稿；"
                    "相同内容摘要重复保存是幂等的。仅写草稿，不发布。"
                ),
            ),
        )
    )


def _elapsed_ms(started: float) -> int:
    """Return a non-negative elapsed millisecond count."""
    return max(0, int((time.perf_counter() - started) * 1000))


def _json_safe(value: Any) -> Any:
    """Convert arguments to JSON-safe values, redacting and capping strings."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)[:ARGUMENT_PREVIEW_CHARS]
    return value


def _result_summary(status: str, error_code: str | None, result_count: int, truncated: bool) -> str:
    """Return a compact, secret-free summary for the tool call record."""
    parts = [
        f"status={status}",
        f"result_count={result_count}",
        f"truncated={str(truncated).lower()}",
    ]
    if error_code:
        parts.append(f"error_code={error_code}")
    return ", ".join(parts)


def _validation_message(exc: ValidationError) -> str:
    """Return a short, non-sensitive description of the first schema error."""
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "input"
    return f"{location}: {first.get('msg', 'invalid input')}"


def _session_has_pending_writes(session: Session) -> bool:
    """Detect whether a read-only handler staged any persistence change."""
    return bool(session.new or session.dirty or session.deleted)


def _validate_arguments(model: type[BaseModel], arguments: dict[str, Any]) -> BaseModel:
    """Validate tool arguments whether they arrive as Python values or JSON-decoded ones.

    Model-generated arguments are always JSON-shaped, so timestamps arrive as strings. The
    Python path is strict and rejects those, while Pydantic's JSON path accepts them because
    JSON has no native datetime. In-process callers keep hitting the strict Python path, and
    both paths still reject unknown fields and wrong types.
    """
    try:
        return model.model_validate(arguments)
    except ValidationError:
        return model.model_validate_json(json.dumps(arguments, default=str))


class ToolRegistry:
    """Dispatches tool invocations through the whitelist and security gates."""

    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        self._specs: dict[str, ToolSpec] = {spec.name: spec for spec in specs}

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Return the registered tool names in insertion order."""
        return tuple(self._specs)

    def get(self, name: str) -> ToolSpec | None:
        """Return a tool spec by name, or None when it is not whitelisted."""
        return self._specs.get(name)

    def invoke(
        self,
        name: str,
        ctx: ToolContext,
        session: Session,
        arguments: Mapping[str, Any],
    ) -> ToolResult[Any]:
        """Validate, authorize, execute, and record one tool invocation."""
        started = time.perf_counter()
        spec = self._specs.get(name)
        if spec is None:
            return self._failure(
                session,
                ctx,
                name,
                arguments,
                started,
                TOOL_INVALID_ARGUMENT,
                f"unknown tool: {name}",
                "failed",
            )
        if name not in ctx.allowed_tools:
            return self._failure(
                session,
                ctx,
                name,
                arguments,
                started,
                TOOL_ACCESS_DENIED,
                "tool is not enabled for this agent run",
                "denied",
            )
        if ctx.tool_call_count >= ctx.max_tool_calls:
            return self._failure(
                session,
                ctx,
                name,
                arguments,
                started,
                TOOL_BUDGET_EXCEEDED,
                "tool call budget exhausted",
                "failed",
            )
        try:
            payload = _validate_arguments(spec.input_model, dict(arguments))
        except ValidationError as exc:
            return self._failure(
                session,
                ctx,
                name,
                arguments,
                started,
                TOOL_INVALID_ARGUMENT,
                _validation_message(exc),
                "failed",
            )
        try:
            outcome = spec.handler(session, ctx, payload)
            if spec.read_only and _session_has_pending_writes(session):
                raise ToolExecutionError(
                    TOOL_INTERNAL_ERROR,
                    "read-only tool attempted to write",
                )
        except ToolExecutionError as exc:
            return self._failure(
                session,
                ctx,
                name,
                arguments,
                started,
                exc.code,
                exc.message,
                "denied" if exc.code == TOOL_ACCESS_DENIED else "failed",
                discard_writes=spec.read_only,
            )
        except Exception as exc:  # noqa: BLE001 - internal errors map to a stable code
            return self._failure(
                session,
                ctx,
                name,
                arguments,
                started,
                TOOL_INTERNAL_ERROR,
                str(exc),
                "failed",
                discard_writes=spec.read_only,
            )
        latency_ms = _elapsed_ms(started)
        self._record(
            session,
            ctx,
            name,
            arguments,
            status="success",
            error_code=None,
            latency_ms=latency_ms,
            result_count=outcome.result_count,
            truncated=outcome.truncated,
        )
        meta = ToolMeta(
            tool_name=name,
            latency_ms=latency_ms,
            truncated=outcome.truncated,
            result_count=outcome.result_count,
        )
        return ToolResult[Any](ok=True, data=outcome.data, error=None, meta=meta)

    def _failure(
        self,
        session: Session,
        ctx: ToolContext,
        name: str,
        arguments: Mapping[str, Any],
        started: float,
        code: str,
        message: str,
        status: str,
        *,
        discard_writes: bool = False,
    ) -> ToolResult[Any]:
        """Build a failed envelope and persist the recorded invocation.

        `discard_writes` is set for read-only tools: detecting the write is not enough
        because `_record()` commits the session, which would flush whatever the handler
        staged. Rolling back first keeps the "read-only tools never write" invariant true
        instead of merely observable, and it also covers a handler that raises after
        staging a write.
        """
        if discard_writes:
            session.rollback()
        latency_ms = _elapsed_ms(started)
        safe_message = redact_text(message)[:ERROR_MESSAGE_CHARS]
        self._record(
            session,
            ctx,
            name,
            arguments,
            status=status,
            error_code=code,
            latency_ms=latency_ms,
            result_count=0,
            truncated=False,
        )
        meta = ToolMeta(tool_name=name, latency_ms=latency_ms, truncated=False, result_count=0)
        error = ToolError(
            code=code,
            message=safe_message,
            retryable=code in RETRYABLE_ERROR_CODES,
        )
        return ToolResult[Any](ok=False, data=None, error=error, meta=meta)

    def _record(
        self,
        session: Session,
        ctx: ToolContext,
        name: str,
        arguments: Mapping[str, Any],
        *,
        status: str,
        error_code: str | None,
        latency_ms: int,
        result_count: int,
        truncated: bool,
    ) -> None:
        """Persist one tool_calls row so every invocation is auditable."""
        record = ToolCall(
            agent_run_id=ctx.agent_run_id,
            tool_name=name,
            arguments_json=_json_safe(arguments),
            result_summary=_result_summary(status, error_code, result_count, truncated),
            status=status,
            error_code=error_code,
            latency_ms=latency_ms,
        )
        session.add(record)
        session.commit()
