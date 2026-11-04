"""工具的白名单注册表、ACL 与预算校验，以及结果信封处理。"""

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
    """一个已注册的工具：它的 schema、处理器，以及是否只读的分类。

    `description` 是白名单以 JSON schema 暴露给模型时、模型读到的那行
    说明 —— 它写给模型看，不是给人看的；必须与
    `docs/agent-tool-contract.md` 保持一致。
    """

    name: str
    read_only: bool
    input_model: type[BaseModel]
    handler: ToolHandler
    description: str = ""


def build_default_registry() -> ToolRegistry:
    """构建持有全部白名单工具的注册表。"""
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
                    "返回值始终标记为不可信外部文本。可给 query_hint 指定关注点，"
                    "长 README 会按相关性优先返回最匹配的片段而非只截取开头。只读。"
                ),
            ),
            ToolSpec(
                name="search_prior_reports",
                read_only=True,
                input_model=SearchPriorReportsInput,
                handler=search_prior_reports,
                description=(
                    "查询授权主题下已发布的历史报告摘要，用于建立对比基线。"
                    "可给 query 指定关注点，按相关性而非时间召回最匹配的往期结论。"
                    "只读，受主题 ACL 限制。"
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
    """返回非负的耗时毫秒数。"""
    return max(0, int((time.perf_counter() - started) * 1000))


def _json_safe(value: Any) -> Any:
    """把参数转成可 JSON 序列化的值，并对字符串做脱敏与截断。"""
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
    """返回给工具调用记录使用的紧凑摘要，不含任何敏感信息。"""
    parts = [
        f"status={status}",
        f"result_count={result_count}",
        f"truncated={str(truncated).lower()}",
    ]
    if error_code:
        parts.append(f"error_code={error_code}")
    return ", ".join(parts)


def _validation_message(exc: ValidationError) -> str:
    """返回第一条 schema 错误的简短、非敏感描述。"""
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "input"
    return f"{location}: {first.get('msg', 'invalid input')}"


def _session_has_pending_writes(session: Session) -> bool:
    """检测只读处理器是否暂存了任何持久化改动。"""
    return bool(session.new or session.dirty or session.deleted)


def _validate_arguments(model: type[BaseModel], arguments: dict[str, Any]) -> BaseModel:
    """校验工具入参，无论它以 Python 值还是以 JSON 解码后的形式到来。

    模型生成的参数总是 JSON 形状，因此时间戳会以字符串形式到达。Python 校验
    路径是严格的，会拒绝这种字符串；而 Pydantic 的 JSON 路径会接受它，因为
    JSON 没有原生的 datetime 类型。进程内调用方仍然走严格的 Python 路径，
    两条路径都照旧拒绝未知字段与错误类型。
    """
    try:
        return model.model_validate(arguments)
    except ValidationError:
        return model.model_validate_json(json.dumps(arguments, default=str))


class ToolRegistry:
    """让工具调用穿过白名单与各项安全闸门后派发执行。"""

    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        self._specs: dict[str, ToolSpec] = {spec.name: spec for spec in specs}

    @property
    def tool_names(self) -> tuple[str, ...]:
        """按插入顺序返回已注册的工具名。"""
        return tuple(self._specs)

    def get(self, name: str) -> ToolSpec | None:
        """按名字取工具规格；不在白名单内时返回 `None`。"""
        return self._specs.get(name)

    def invoke(
        self,
        name: str,
        ctx: ToolContext,
        session: Session,
        arguments: Mapping[str, Any],
    ) -> ToolResult[Any]:
        """校验、授权、执行并记录一次工具调用。"""
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
        except Exception as exc:  # noqa: BLE001 - 内部异常要映射成稳定错误码
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
        """构造失败信封，并把这次调用记录下来。

        只读工具会置上 `discard_writes`：光检测到写入还不够，因为 `_record()`
        会提交会话，那样就会把处理器暂存的写入冲刷落库。先回滚，才能让
        「只读工具绝不写入」这条不变量真正成立，而不只是可被观察到；它也覆盖
        了「处理器暂存了写入、随后又抛异常」的情况。
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
        """持久化一行 `tool_calls`，让每次调用都可审计。"""
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
        # 所属步骤在调用返回之后才写入，因此这个 id 稍后由
        # `StepRecorder.link_tool_calls` 认领。若留空，运行页始终无法把调用
        # 显示在它所属的步骤下，只能把所有调用都丢进
        # 「未挂到步骤上的工具调用」里。
        ctx.recorded_call_ids.append(record.id)
