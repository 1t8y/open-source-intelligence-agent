"""维护 `PipelineRun` 生命周期、错误归类与运行历史查询。

每个确定性阶段或 Agent 阶段都会写一条运行记录，运维无需翻日志就能回答
「跑了什么、何时跑、结果是什么、为什么失败」。状态一改变就立即提交运行记录，
因为失败的进程不能把「它失败过」的证据一并丢掉。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.collectors.types import CollectorError
from open_source_intelligence.core.logging import redact_text
from open_source_intelligence.persistence.models.intelligence import PipelineRun

GLOBAL_RUN_TYPES: Final[tuple[str, ...]] = ("collect", "snapshot", "rank", "contents", "backfill")
TOPIC_RUN_TYPES: Final[tuple[str, ...]] = ("agent", "publish", "push")
RUN_TYPES: Final[tuple[str, ...]] = GLOBAL_RUN_TYPES + TOPIC_RUN_TYPES
RUN_STATUSES: Final[tuple[str, ...]] = ("queued", "running", "succeeded", "failed", "partial")
TERMINAL_STATUSES: Final[tuple[str, ...]] = ("succeeded", "failed", "partial")

ERROR_COLLECTOR_FAILED: Final[str] = "COLLECTOR_FAILED"
ERROR_SNAPSHOT_FAILED: Final[str] = "SNAPSHOT_FAILED"
ERROR_RANK_FAILED: Final[str] = "RANK_FAILED"
ERROR_CONTENT_FAILED: Final[str] = "CONTENT_FETCH_FAILED"
ERROR_UNEXPECTED: Final[str] = "PIPELINE_UNEXPECTED_ERROR"

STAGE_ERROR_CODES: Final[dict[str, str]] = {
    "collect": ERROR_COLLECTOR_FAILED,
    "snapshot": ERROR_SNAPSHOT_FAILED,
    "rank": ERROR_RANK_FAILED,
    "contents": ERROR_CONTENT_FAILED,
}
# `backfill` 刻意不在其中：`classify_error` 已经把每个 `CollectorError` 映射到
# `COLLECTOR_FAILED`，因此一次因其它原因失败的遍历会落到 `PIPELINE_UNEXPECTED_ERROR`。
# 把运行类型本身也映射进来，会把「源头拒绝」和「我们自己的写入坏了」压成同一个错误码，
# 而这恰恰是运维最需要区分的东西。

MAX_ERROR_MESSAGE_LENGTH: Final[int] = 2000


@dataclass(frozen=True, slots=True)
class PipelineRunSummary:
    """单条流水线运行记录的可序列化视图，供 CLI、API 与日志输出使用。"""

    run_id: int
    topic_id: int | None
    run_type: str
    status: str
    attempt: int
    started_at: str | None
    finished_at: str | None
    metrics: dict[str, Any]
    error_code: str | None
    error_message: str | None

    def to_dict(self) -> dict[str, Any]:
        """返回可供 CLI、API 与日志输出使用的 JSON 载荷。"""
        return {
            "run_id": self.run_id,
            "topic_id": self.topic_id,
            "run_type": self.run_type,
            "status": self.status,
            "attempt": self.attempt,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metrics": self.metrics,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """一个编排器阶段记录下来的结果。

    它放在运行记录辅助函数旁边，而不是放在最先需要它的模块里，因为 `contents` 阶段与
    每日编排器都会产出它：若由其中一个模块定义，另一个就得纯粹为了这个类型去导入它。
    """

    run_id: int
    run_type: str
    topic_id: int | None
    status: str
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """返回可直接转为 JSON 的载荷。"""
        return {
            "run_id": self.run_id,
            "run_type": self.run_type,
            "topic_id": self.topic_id,
            "status": self.status,
            "metrics": self.metrics,
        }


class PipelineStageError(RuntimeError):
    """当某个阶段以必须中止整批的方式失败时抛出。

    上下文关键字参数默认取空值，好让 Celery 能从只携带消息的 JSON 结果后端重建这个异常。
    """

    def __init__(
        self,
        message: str,
        *,
        run_id: int = 0,
        run_type: str = "",
        error_code: str = "",
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.run_type = run_type
        self.error_code = error_code


def _utc_now() -> datetime:
    """返回当前带时区的 UTC 时间戳。"""
    return datetime.now(UTC)


def validate_run_type(run_type: str) -> str:
    """拒绝数据库约束同样会拒绝的 `run_type`。"""
    if run_type not in RUN_TYPES:
        raise ValueError(f"unsupported run_type: {run_type}")
    return run_type


def validate_run_status(status: str) -> str:
    """拒绝数据库约束同样会拒绝的状态值。"""
    if status not in RUN_STATUSES:
        raise ValueError(f"unsupported status: {status}")
    return status


def require_topic_scope(run_type: str, topic_id: int | None) -> None:
    """要求 Agent 运行与发布运行绑定主题，与数据库 CHECK 约束一致。"""
    if run_type in TOPIC_RUN_TYPES and topic_id is None:
        raise ValueError(f"run_type {run_type} requires a topic_id")


def classify_error(run_type: str, error: BaseException) -> str:
    """把异常归到稳定的阶段错误码。"""
    if isinstance(error, CollectorError):
        return ERROR_COLLECTOR_FAILED
    return STAGE_ERROR_CODES.get(run_type, ERROR_UNEXPECTED)


def describe_error(error: BaseException) -> str:
    """在异常消息落库前对其脱敏并限长。"""
    message = redact_text(str(error)).strip() or error.__class__.__name__
    return message[:MAX_ERROR_MESSAGE_LENGTH]


def start_pipeline_run(
    session: Session,
    *,
    run_type: str,
    topic_id: int | None = None,
    attempt: int = 1,
    metrics_json: dict[str, Any] | None = None,
) -> PipelineRun:
    """创建一条 `queued` 运行记录并立即落库。"""
    validate_run_type(run_type)
    require_topic_scope(run_type, topic_id)
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    run = PipelineRun(
        topic_id=topic_id,
        run_type=run_type,
        status="queued",
        attempt=attempt,
        metrics_json=metrics_json,
    )
    session.add(run)
    session.commit()
    return run


def mark_pipeline_run_running(
    session: Session,
    run: PipelineRun,
    *,
    started_at: datetime | None = None,
) -> PipelineRun:
    """把 `queued` 状态的运行推进到 `running`。"""
    if run.status in TERMINAL_STATUSES:
        raise ValueError(f"run {run.id} already finished with status {run.status}")
    run.status = "running"
    if run.started_at is None:
        run.started_at = started_at or _utc_now()
    session.commit()
    return run


def finish_pipeline_run(
    session: Session,
    run: PipelineRun,
    *,
    status: str,
    metrics_json: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    finished_at: datetime | None = None,
) -> PipelineRun:
    """以终态、指标与可选错误详情关闭一条运行。

    `finished_at` 是为需要钉住时钟的调用方准备的：运行在他们给定的时刻启动，而
    `pipeline_runs` 要求 `finished_at >= started_at`，若按墙上时钟关闭，记录就会自相矛盾。
    不钉住时钟的调用方留空即可，运行照常按墙上时钟关闭。
    """
    validate_run_status(status)
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"{status} is not a terminal status")
    closed_at = finished_at or _utc_now()
    run.status = status
    if run.started_at is None:
        run.started_at = closed_at
    run.finished_at = closed_at
    if metrics_json:
        merged: dict[str, Any] = dict(run.metrics_json or {})
        merged.update(metrics_json)
        run.metrics_json = merged
    if error_code is not None:
        run.error_code = error_code[:64]
    if error_message is not None:
        run.error_message = redact_text(error_message)[:MAX_ERROR_MESSAGE_LENGTH]
    session.commit()
    return run


def fail_pipeline_run(
    session: Session,
    run: PipelineRun,
    *,
    error_code: str,
    error_message: str,
    metrics_json: dict[str, Any] | None = None,
    finished_at: datetime | None = None,
) -> PipelineRun:
    """以稳定的错误码与脱敏后的消息把运行关闭为失败。"""
    return finish_pipeline_run(
        session,
        run,
        status="failed",
        metrics_json=metrics_json,
        error_code=error_code,
        error_message=error_message,
        finished_at=finished_at,
    )


def get_pipeline_run(session: Session, run_id: int) -> PipelineRun | None:
    """按主键返回一条运行记录。"""
    return session.get(PipelineRun, run_id)


def list_pipeline_runs(
    session: Session,
    *,
    topic_id: int | None = None,
    run_type: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> Sequence[PipelineRun]:
    """返回最近的运行记录，新的在前，可按可选条件筛选。"""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    statement = select(PipelineRun)
    if topic_id is not None:
        statement = statement.where(PipelineRun.topic_id == topic_id)
    if run_type is not None:
        statement = statement.where(PipelineRun.run_type == validate_run_type(run_type))
    if status is not None:
        statement = statement.where(PipelineRun.status == validate_run_status(status))
    statement = statement.order_by(PipelineRun.id.desc()).limit(limit)
    return session.scalars(statement).all()


@dataclass(frozen=True)
class PublishDecision:
    """一份报告最近一次发布策略裁决的可展示摘要。

    `metrics_json` 里 `stage="evaluate"` 的那条运行才是裁决本身；同一份报告后续的
    `stage="publish"` / `stage="push"` 只是执行结果，不含 `reason_codes`。
    """

    run_id: int
    report_id: int
    resolution: str
    reason_codes: tuple[str, ...]


def latest_publish_decisions(
    session: Session,
    *,
    report_ids: Sequence[int],
    scan_limit: int = 500,
) -> dict[int, PublishDecision]:
    """按报告取最近一次发布裁决，供界面解释「为什么还没发」。

    裁决的 `report_id` 只存在 `metrics_json` 里，跨 MySQL / SQLite 用 SQL 的 JSON 函数过滤
    会踩方言差异，所以改为一次取回最近的 publish 运行、在 Python 里归组。倒序遍历并只保留
    每个报告第一次出现的那条，天然是「最近一次」。`scan_limit` 只是给运维表设个上界，
    平时的每日链路每天每主题只写一条裁决。
    """
    wanted = set(report_ids)
    if not wanted:
        return {}
    runs = session.scalars(
        select(PipelineRun)
        .where(PipelineRun.run_type == "publish")
        .order_by(PipelineRun.id.desc())
        .limit(scan_limit)
    ).all()
    decisions: dict[int, PublishDecision] = {}
    for run in runs:
        metrics = dict(run.metrics_json or {})
        if metrics.get("stage") != "evaluate":
            continue
        report_id = metrics.get("report_id")
        if not isinstance(report_id, int) or report_id not in wanted or report_id in decisions:
            continue
        reasons = metrics.get("reason_codes") or []
        decisions[report_id] = PublishDecision(
            run_id=int(run.id),
            report_id=report_id,
            resolution=str(metrics.get("resolution") or ""),
            reason_codes=tuple(str(reason) for reason in reasons),
        )
    return decisions


def summarize_pipeline_run(run: PipelineRun) -> PipelineRunSummary:
    """把运行记录转换成可序列化的摘要。"""
    return PipelineRunSummary(
        run_id=run.id,
        topic_id=run.topic_id,
        run_type=run.run_type,
        status=run.status,
        attempt=run.attempt,
        started_at=run.started_at.isoformat() if run.started_at is not None else None,
        finished_at=run.finished_at.isoformat() if run.finished_at is not None else None,
        metrics=dict(run.metrics_json or {}),
        error_code=run.error_code,
        error_message=run.error_message,
    )


def summarize_pipeline_runs(runs: Sequence[PipelineRun]) -> list[dict[str, Any]]:
    """把多条运行记录转换成可直接转为 JSON 的载荷。"""
    return [summarize_pipeline_run(run).to_dict() for run in runs]
