"""编排每日确定性流水线，每个阶段写一条 `PipelineRun`。

顺序固定且由规则驱动：`collect` -> `snapshot` -> `rank` -> `contents`。全程不涉及语言模型，
因此相同输入重跑会得到相同的候选排名。每个阶段各写自己的运行记录，一批部分失败也能不读
容器日志就完成诊断。

`contents` 是最后一个阶段，也是唯一可选的阶段：它为排好序的候选预热 README 缓存，而且必须
有网络来源才做得了事。不传内容来源的调用方只会得到三个确定性阶段、没有 `contents` 运行
记录 —— 这正是「没什么可抓」的一次运行应有的样子。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.collectors.service import collect_repositories
from open_source_intelligence.collectors.types import (
    RepositoryCollector,
    RepositoryContentSource,
)
from open_source_intelligence.persistence.models.intelligence import Topic
from open_source_intelligence.pipeline.contents import run_contents_stage
from open_source_intelligence.pipeline.ranking import rank_topic_candidates
from open_source_intelligence.pipeline.runs import (
    ERROR_COLLECTOR_FAILED,
    PipelineStageError,
    StageOutcome,
    classify_error,
    describe_error,
    fail_pipeline_run,
    finish_pipeline_run,
    mark_pipeline_run_running,
    start_pipeline_run,
)
from open_source_intelligence.pipeline.snapshots import create_daily_snapshots


@dataclass(frozen=True, slots=True)
class CollectStageOutcome:
    """采集阶段的结果，附带各 provider 的计数。"""

    stage: StageOutcome
    provider_counts: dict[str, int]
    provider_errors: dict[str, str]


@dataclass(frozen=True, slots=True)
class DailyPipelineResult:
    """一次每日确定性流水线执行的汇总结果。"""

    business_date: str
    attempt: int
    status: str
    stages: list[StageOutcome]
    provider_counts: dict[str, int]
    provider_errors: dict[str, str]
    snapshot_count: int
    topic_candidate_counts: dict[str, int]
    content_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """返回可供 CLI、Worker 与日志输出使用的 JSON 载荷。"""
        return {
            "business_date": self.business_date,
            "attempt": self.attempt,
            "status": self.status,
            "run_ids": [stage.run_id for stage in self.stages],
            "stages": [stage.to_dict() for stage in self.stages],
            "provider_counts": self.provider_counts,
            "provider_errors": self.provider_errors,
            "snapshot_count": self.snapshot_count,
            "topic_candidate_counts": self.topic_candidate_counts,
            "content_counts": self.content_counts,
        }


def list_enabled_topics(session: Session) -> Sequence[Topic]:
    """按稳定顺序返回已启用的主题。"""
    return session.scalars(select(Topic).where(Topic.enabled.is_(True)).order_by(Topic.id)).all()


def run_collect_stage(
    session: Session,
    *,
    business_date: date,
    collectors: Sequence[RepositoryCollector],
    limit: int,
    attempt: int = 1,
) -> CollectStageOutcome:
    """向每个被请求的 provider 采集，并记录这一批。

    只要还有另一个 provider 返回数据，单个 provider 的失败就会被记录并容忍。只有所有
    provider 都失败的一批才算致命，因为空的采集会让下游各个切片失去意义。
    """
    if not collectors:
        raise ValueError("at least one collector is required")
    run = start_pipeline_run(session, run_type="collect", topic_id=None, attempt=attempt)
    mark_pipeline_run_running(session, run)
    try:
        summary = collect_repositories(session, collectors, limit=limit)
    except Exception as exc:  # noqa: BLE001 - 记录后重新抛出为阶段错误
        # provider 层面的失败是 `CollectorError`，已经按 provider 汇总过了。其它任何异常
        # （`limit` 不合法、upsert 时的数据库错误）都会逃出服务层，没有这个分支，运行行会
        # 一直停在 `running` —— 正是这个状态让死掉的批次与活着的批次无法区分。
        error_code = classify_error("collect", exc)
        message = describe_error(exc)
        fail_pipeline_run(session, run, error_code=error_code, error_message=message)
        raise PipelineStageError(
            message,
            run_id=run.id,
            run_type="collect",
            error_code=error_code,
        ) from exc
    provider_counts = dict(summary.provider_counts)
    provider_errors = dict(summary.errors)
    metrics: dict[str, Any] = {
        "business_date": business_date.isoformat(),
        "provider_counts": provider_counts,
        "provider_errors": provider_errors,
        "limit": limit,
    }
    if len(provider_errors) == len(collectors):
        message = "; ".join(
            f"{provider}: {error}" for provider, error in sorted(provider_errors.items())
        )
        fail_pipeline_run(
            session,
            run,
            error_code=ERROR_COLLECTOR_FAILED,
            error_message=message,
            metrics_json=metrics,
        )
        raise PipelineStageError(
            message,
            run_id=run.id,
            run_type="collect",
            error_code=ERROR_COLLECTOR_FAILED,
        )
    status = "partial" if provider_errors else "succeeded"
    finish_pipeline_run(session, run, status=status, metrics_json=metrics)
    return CollectStageOutcome(
        stage=StageOutcome(
            run_id=run.id,
            run_type="collect",
            topic_id=None,
            status=status,
            metrics=metrics,
        ),
        provider_counts=provider_counts,
        provider_errors=provider_errors,
    )


def run_snapshot_stage(
    session: Session,
    *,
    business_date: date,
    attempt: int = 1,
) -> StageOutcome:
    """创建或刷新每日快照，并记录本次运行。"""
    run = start_pipeline_run(session, run_type="snapshot", topic_id=None, attempt=attempt)
    mark_pipeline_run_running(session, run)
    try:
        snapshot_count = create_daily_snapshots(session, snapshot_date=business_date)
    except Exception as exc:  # noqa: BLE001 - 记录后重新抛出为阶段错误
        error_code = classify_error("snapshot", exc)
        message = describe_error(exc)
        fail_pipeline_run(session, run, error_code=error_code, error_message=message)
        raise PipelineStageError(
            message,
            run_id=run.id,
            run_type="snapshot",
            error_code=error_code,
        ) from exc
    metrics: dict[str, Any] = {
        "snapshot_count": snapshot_count,
        "snapshot_date": business_date.isoformat(),
    }
    finish_pipeline_run(session, run, status="succeeded", metrics_json=metrics)
    return StageOutcome(
        run_id=run.id,
        run_type="snapshot",
        topic_id=None,
        status="succeeded",
        metrics=metrics,
    )


def run_rank_stage(
    session: Session,
    *,
    topic: Topic,
    business_date: date,
    attempt: int = 1,
) -> StageOutcome:
    """为一个主题排序并记录本次运行，但不因此让整批失败。"""
    run = start_pipeline_run(session, run_type="rank", topic_id=topic.id, attempt=attempt)
    mark_pipeline_run_running(session, run)
    try:
        ranked = rank_topic_candidates(session, topic, as_of_date=business_date)
    except Exception as exc:  # noqa: BLE001 - 一个主题不能拦住其它主题
        error_code = classify_error("rank", exc)
        message = describe_error(exc)
        fail_pipeline_run(session, run, error_code=error_code, error_message=message)
        return StageOutcome(
            run_id=run.id,
            run_type="rank",
            topic_id=topic.id,
            status="failed",
            metrics={"error_code": error_code, "as_of_date": business_date.isoformat()},
        )
    metrics: dict[str, Any] = {
        "candidate_count": len(ranked),
        "selected_count": sum(1 for candidate in ranked if candidate.selected_for_agent),
        "as_of_date": business_date.isoformat(),
    }
    finish_pipeline_run(session, run, status="succeeded", metrics_json=metrics)
    return StageOutcome(
        run_id=run.id,
        run_type="rank",
        topic_id=topic.id,
        status="succeeded",
        metrics=metrics,
    )


def run_rank_all_topics(
    session: Session,
    *,
    business_date: date,
    attempt: int = 1,
) -> list[StageOutcome]:
    """为每个启用主题排序，每个主题一条运行记录。"""
    return [
        run_rank_stage(session, topic=topic, business_date=business_date, attempt=attempt)
        for topic in list_enabled_topics(session)
    ]


def run_daily_pipeline(
    session: Session,
    *,
    business_date: date,
    collectors: Sequence[RepositoryCollector],
    limit: int,
    content_sources: Sequence[RepositoryContentSource] = (),
    content_limit: int | None = None,
    content_refresh_after: datetime | None = None,
    attempt: int = 1,
) -> DailyPipelineResult:
    """把 `collect`、`snapshot`、`rank` 与（可选的）`contents` 作为一批可观测任务执行。

    `content_sources` 默认为空，而空列表意味着 `contents` 阶段完全不跑：不发请求、不写
    运行记录、`content_counts` 为空。设这个默认值，是因为内容阶段是唯一会访问「按仓库」
    端点的阶段；拿不出这种来源的调用方（测试夹具、演练、离线回放）应当得到三个确定性
    阶段，而不是一个报告零成功的阶段。

    `content_limit` 默认取 `limit` —— 与采集批次同一数量级 —— 这样没特意考虑过的调用方，
    拿到的请求数也是有界的。

    Raises:
        PipelineStageError: 采集、快照或内容阶段本身失败时。排序失败留在受影响的主题
            内部，记为一条失败的 `rank` 运行；单个仓库抓不到内容只计数，不抛出。
    """
    stages: list[StageOutcome] = []
    collect_outcome = run_collect_stage(
        session,
        business_date=business_date,
        collectors=collectors,
        limit=limit,
        attempt=attempt,
    )
    stages.append(collect_outcome.stage)
    snapshot_stage = run_snapshot_stage(session, business_date=business_date, attempt=attempt)
    stages.append(snapshot_stage)
    rank_stages = run_rank_all_topics(session, business_date=business_date, attempt=attempt)
    stages.extend(rank_stages)

    content_counts: dict[str, int] = {}
    if content_sources:
        contents_outcome = run_contents_stage(
            session,
            business_date=business_date,
            sources=content_sources,
            limit=limit if content_limit is None else content_limit,
            refresh_after=content_refresh_after,
            attempt=attempt,
        )
        stages.append(contents_outcome.stage)
        summary = contents_outcome.summary
        content_counts = {
            "candidate_count": contents_outcome.candidate_count,
            "fetched": summary.fetched,
            "skipped": summary.skipped,
            "missing": summary.missing,
            "unsupported": summary.unsupported,
            "failed": summary.failed,
        }

    stage_statuses = {stage.status for stage in stages}
    overall_status = "partial" if stage_statuses & {"failed", "partial"} else "succeeded"
    snapshot_count = int(snapshot_stage.metrics.get("snapshot_count") or 0)
    return DailyPipelineResult(
        business_date=business_date.isoformat(),
        attempt=attempt,
        status=overall_status,
        stages=stages,
        provider_counts=collect_outcome.provider_counts,
        provider_errors=collect_outcome.provider_errors,
        snapshot_count=snapshot_count,
        topic_candidate_counts={
            str(stage.topic_id): int(stage.metrics.get("candidate_count") or 0)
            for stage in rank_stages
            if stage.status == "succeeded"
        },
        content_counts=content_counts,
    )
