"""回填走查：一页一页地走，每页之后提交一次游标。

下面的一切都由两条性质塑造。第一，走查是*可续跑*的：游标按页持久化，所
以一次中断只损失正在飞的那一页，而不是整段历史。第二，走查是*有节流*
的：回填是本系统里唯一会连着向公共 API 问同一个问题五十次的部分，于是数
据源行从 T4 起就带着的请求预算终于被花掉了。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from open_source_intelligence.backfill.errors import (
    BackfillUnsupportedCollector,
    BackfillWalkFailed,
)
from open_source_intelligence.backfill.jobs import (
    FAILED,
    SUCCEEDED,
    advance_job,
    claim_job,
    create_job,
    finish_job,
    require_job,
)
from open_source_intelligence.collectors.limiter import RateLimiter
from open_source_intelligence.collectors.service import ensure_data_source, ingest_items
from open_source_intelligence.collectors.types import (
    CollectorError,
    PaginatedRepositoryCollector,
    RepositoryCollector,
)
from open_source_intelligence.persistence.models.intelligence import BackfillJob
from open_source_intelligence.pipeline.runs import (
    classify_error,
    describe_error,
    fail_pipeline_run,
    finish_pipeline_run,
    mark_pipeline_run_running,
    start_pipeline_run,
)
from open_source_intelligence.security.audit import record_audit_event

BACKFILL_RUN_TYPE = "backfill"


@dataclass(frozen=True, slots=True)
class PageRecord:
    """走查中一页产出了什么。"""

    page: int
    item_count: int
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        """返回可 JSON 序列化的载荷。"""
        return {"page": self.page, "item_count": self.item_count, "has_more": self.has_more}


@dataclass(frozen=True, slots=True)
class BackfillOutcome:
    """一次回填走查的汇总结果。"""

    job_id: int
    run_id: int
    provider: str
    status: str
    attempt: int
    started_page: int
    next_page: int
    target_pages: int
    per_page: int
    pages_this_attempt: int
    items_this_attempt: int
    pages_completed: int
    items_collected: int
    exhausted: bool
    paced_seconds: float
    pages: tuple[PageRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        """返回供 CLI、Worker 与日志输出使用的、可 JSON 序列化的载荷。"""
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "provider": self.provider,
            "status": self.status,
            "attempt": self.attempt,
            "started_page": self.started_page,
            "next_page": self.next_page,
            "target_pages": self.target_pages,
            "per_page": self.per_page,
            "pages_this_attempt": self.pages_this_attempt,
            "items_this_attempt": self.items_this_attempt,
            "pages_completed": self.pages_completed,
            "items_collected": self.items_collected,
            "exhausted": self.exhausted,
            "paced_seconds": round(self.paced_seconds, 4),
            "pages": [record.to_dict() for record in self.pages],
        }


def require_paginated(collector: RepositoryCollector) -> PaginatedRepositoryCollector:
    """把采集器当作分页采集器返回，或说明它无法分页。"""
    if not isinstance(collector, PaginatedRepositoryCollector):
        raise BackfillUnsupportedCollector(
            f"collector '{collector.provider}' cannot be walked page by page"
        )
    return collector


def run_backfill(
    session: Session,
    *,
    job: BackfillJob,
    collector: RepositoryCollector,
    limiter: RateLimiter | None = None,
    now: datetime | None = None,
) -> BackfillOutcome:
    """走查一个作业剩余的页，并在每页之后提交游标。

    游标按页提交而不是到最后才提交，这正是全部要点：一次在第 7 页（共
    20 页）之后被杀掉的走查，会留下 `next_page` 为 8 的作业，下一次运行
    就从那里开始。只在成功时提交一次，会让每次中断都变成完整重放。

    一页的仓库与指向它们之后的游标在同一个事务里提交。入库对
    (source, provider_repo_id) 是幂等的，所以重放本来也不会重复一行 ——
    但这让崩溃窗口变得无害，而不只是「可以扛过去」，也意味着一个前进过
    的游标，永远对应一页真的落了地。

    `limiter` 默认按数据源自己的 `rate_limit_per_minute` 构造，于是源
    行上配置的预算就是真正被执行的预算。传一个进去，是测试用注入时钟而
    不是真实 sleep 来做节流的方式。`now` 只固定边界 —— 认领与收尾 ——
    每页的心跳仍然来自墙上时钟，因为一个被调用方冻住的心跳，会看起来像
    死掉的走查者。

    抛出：
        BackfillUnsupportedCollector：源无法分页时。
        BackfillJobBusy：另一个走查仍持有游标时。
        BackfillNotResumable：作业已经没有剩余页时。
        BackfillWalkFailed：某一页失败时。作业行与运行行已经记录了
            失败，以及游标停在哪一页。
    """
    paginated = require_paginated(collector)
    source = ensure_data_source(session, collector)
    claim_job(session, job, now=now)

    run = start_pipeline_run(
        session,
        run_type=BACKFILL_RUN_TYPE,
        attempt=job.attempt,
        metrics_json={"backfill_job_id": job.id, "provider": job.provider},
    )
    mark_pipeline_run_running(session, run, started_at=now)
    if limiter is None:
        limiter = RateLimiter(per_minute=source.rate_limit_per_minute)

    started_page = job.next_page
    pages: list[PageRecord] = []
    items_this_attempt = 0
    exhausted = False
    # 以差值度量，而不是直接从限速器读取：一个限速器可能活过一次走查之后
    # （续跑复用同一个对象就是最明显的情形），而一个报告累计总量的结果会把
    # 上一次的等待算到这一次头上。
    paced_before = limiter.total_waited_seconds
    try:
        while job.next_page <= job.target_pages:
            limiter.wait()
            page = paginated.collect_page(page=job.next_page, per_page=job.per_page)
            ingest_items(session, source=source, items=page.items)
            record = PageRecord(page=page.page, item_count=len(page.items), has_more=page.has_more)
            pages.append(record)
            items_this_attempt += record.item_count
            advance_job(session, job, page=page.page, item_count=record.item_count)
            if not page.has_more:
                exhausted = True
                break
    except CollectorError as exc:
        error_code = classify_error(BACKFILL_RUN_TYPE, exc)
        message = describe_error(exc)
        metrics = _progress_metrics(job, pages, items_this_attempt, exhausted)
        finish_job(
            session, job, status=FAILED, error_code=error_code, error_message=message, now=now
        )
        # 审计行随运行记录一起提交，于是一次失败的走查会留下两条记录，而不
        # 必再开一个自己也可能失败的事务。
        record_audit_event(
            session,
            action="backfill.run",
            resource_type="backfill_job",
            resource_id=str(job.id),
            result="failed",
            metadata_json={**metrics, "error_code": error_code},
        )
        fail_pipeline_run(
            session,
            run,
            error_code=error_code,
            error_message=message,
            metrics_json=metrics,
            finished_at=now,
        )
        raise BackfillWalkFailed(
            message,
            job_id=job.id,
            run_id=run.id,
            error_code=error_code,
            next_page=job.next_page,
        ) from exc

    metrics = _progress_metrics(job, pages, items_this_attempt, exhausted)
    paced_this_attempt = limiter.total_waited_seconds - paced_before
    metrics["paced_seconds"] = round(paced_this_attempt, 4)
    finish_job(session, job, status=SUCCEEDED, now=now)
    record_audit_event(
        session,
        action="backfill.run",
        resource_type="backfill_job",
        resource_id=str(job.id),
        result="success",
        metadata_json=metrics,
    )
    finish_pipeline_run(session, run, status=SUCCEEDED, metrics_json=metrics, finished_at=now)
    return BackfillOutcome(
        job_id=job.id,
        run_id=run.id,
        provider=job.provider,
        status=job.status,
        attempt=job.attempt,
        started_page=started_page,
        next_page=job.next_page,
        target_pages=job.target_pages,
        per_page=job.per_page,
        pages_this_attempt=len(pages),
        items_this_attempt=items_this_attempt,
        pages_completed=job.pages_completed,
        items_collected=job.items_collected,
        exhausted=exhausted,
        paced_seconds=paced_this_attempt,
        pages=tuple(pages),
    )


def start_backfill(
    session: Session,
    *,
    collector: RepositoryCollector,
    target_pages: int,
    per_page: int,
    limiter: RateLimiter | None = None,
    now: datetime | None = None,
) -> BackfillOutcome:
    """为一个源创建作业，并在一次调用里走完它。"""
    paginated = require_paginated(collector)
    job = create_job(
        session,
        provider=paginated.provider,
        query=paginated.describe_query(),
        target_pages=target_pages,
        per_page=per_page,
        now=now,
    )
    return run_backfill(session, job=job, collector=paginated, limiter=limiter, now=now)


def resume_backfill(
    session: Session,
    *,
    job_id: int,
    collector: RepositoryCollector,
    limiter: RateLimiter | None = None,
    now: datetime | None = None,
) -> BackfillOutcome:
    """从作业停下的游标处续跑。"""
    return run_backfill(
        session,
        job=require_job(session, job_id),
        collector=collector,
        limiter=limiter,
        now=now,
    )


def _progress_metrics(
    job: BackfillJob,
    pages: list[PageRecord],
    items_this_attempt: int,
    exhausted: bool,
) -> dict[str, Any]:
    """为运行账本描述一次走查的进展。"""
    return {
        "backfill_job_id": job.id,
        "provider": job.provider,
        "target_pages": job.target_pages,
        "per_page": job.per_page,
        "pages_this_attempt": len(pages),
        "items_this_attempt": items_this_attempt,
        "pages_completed": job.pages_completed,
        "items_collected": job.items_collected,
        "next_page": job.next_page,
        "exhausted": exhausted,
        "pages": [record.to_dict() for record in pages],
    }
