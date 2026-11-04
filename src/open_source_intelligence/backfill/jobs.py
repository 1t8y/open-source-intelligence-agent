"""回填作业的生命周期与游标持久化。

作业行*就是*检查点。续跑需要的一切 —— 下一页该请求哪一页、已经入库了
多少 —— 都放在列里，而不是放在正在走查的那个进程里，因为进程才是会死
掉的那一部分。

这里所有时间戳都是带时区的 UTC。列类型在写入时拒绝 naive 值，所以需要
固定时钟的调用方，必须把它固定成一个带时区的时刻，而不是一个裸时刻。

有一个坑值得说一次：`TimestampMixin.updated_at` 无法被固定。它的
Python 侧 `onupdate` 由 SQLAlchemy 在每次 UPDATE 时施加，并且会盖过显
式赋值，因此这一列最终总是拿着墙上时钟。这正是下面的存活信号用
`heartbeat_at` 而不是 `updated_at` 的原因 —— 一个测试控制不了时钟的
守卫，就是一个没人会去测的守卫。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.backfill.errors import (
    BackfillJobBusy,
    BackfillJobNotFound,
    BackfillLimitError,
    BackfillNotResumable,
)
from open_source_intelligence.collectors.types import MAX_PER_PAGE
from open_source_intelligence.core.logging import redact_text
from open_source_intelligence.persistence.models.intelligence import BackfillJob

PENDING: Final[str] = "pending"
RUNNING: Final[str] = "running"
SUCCEEDED: Final[str] = "succeeded"
FAILED: Final[str] = "failed"
CANCELLED: Final[str] = "cancelled"

JOB_STATUSES: Final[tuple[str, ...]] = (PENDING, RUNNING, SUCCEEDED, FAILED, CANCELLED)
TERMINAL_JOB_STATUSES: Final[tuple[str, ...]] = (SUCCEEDED, FAILED, CANCELLED)

DEFAULT_TARGET_PAGES: Final[int] = 5
DEFAULT_PER_PAGE: Final[int] = 50

MAX_TARGET_PAGES: Final[int] = 100
"""单次走查页数预算的上限。

GitHub 的搜索 API 拒绝翻过 1000 条结果的上限，预算再大也只是买到源端根本不会应答的请求。
把预算限制在 `MAX_PER_PAGE` 同一量级，也意味着 `next_page` 永远不会指向一个没有供应商愿意
回答的页码。
"""

STALE_AFTER: Final[timedelta] = timedelta(minutes=15)
"""运行中的作业可以多久不心跳，之后其它走查才能接管它。"""

MAX_ERROR_MESSAGE_LENGTH: Final[int] = 2000


@dataclass(frozen=True, slots=True)
class BackfillJobSummary:
    """一个回填作业的可序列化视图。"""

    job_id: int
    provider: str
    status: str
    attempt: int
    query: dict[str, Any]
    next_page: int
    target_pages: int
    per_page: int
    pages_completed: int
    items_collected: int
    started_at: str | None
    finished_at: str | None
    heartbeat_at: str | None
    error_code: str | None
    error_message: str | None

    @property
    def pages_remaining(self) -> int:
        """返回这次走查还打算请求多少页。"""
        return max(self.target_pages - self.pages_completed, 0)

    @property
    def resumable(self) -> bool:
        """返回再走一次是否会有进展。"""
        return self.status != SUCCEEDED and self.next_page <= self.target_pages

    def to_dict(self) -> dict[str, Any]:
        """返回供 CLI 与 Worker 输出使用的、可 JSON 序列化的载荷。"""
        return {
            "job_id": self.job_id,
            "provider": self.provider,
            "status": self.status,
            "attempt": self.attempt,
            "query": self.query,
            "next_page": self.next_page,
            "target_pages": self.target_pages,
            "per_page": self.per_page,
            "pages_completed": self.pages_completed,
            "items_collected": self.items_collected,
            "pages_remaining": self.pages_remaining,
            "resumable": self.resumable,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "heartbeat_at": self.heartbeat_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


def validate_target_pages(target_pages: int) -> int:
    """拒绝超出 schema 接受的页预算。"""
    if not 1 <= target_pages <= MAX_TARGET_PAGES:
        raise BackfillLimitError(f"target_pages must be between 1 and {MAX_TARGET_PAGES}")
    return target_pages


def validate_per_page(per_page: int) -> int:
    """拒绝超出 schema 与供应商两者接受的页大小。"""
    if not 1 <= per_page <= MAX_PER_PAGE:
        raise BackfillLimitError(f"per_page must be between 1 and {MAX_PER_PAGE}")
    return per_page


def create_job(
    session: Session,
    *,
    provider: str,
    query: dict[str, Any],
    target_pages: int = DEFAULT_TARGET_PAGES,
    per_page: int = DEFAULT_PER_PAGE,
    now: datetime | None = None,
) -> BackfillJob:
    """创建一个停在第一页之前的、状态为 pending 的作业。

    `attempt` 从 1 开始，尽管此时还什么都没走查，因为账本自己的约定是尝
    试次数从 1 计、永不从 0 计。`started_at` 保持为空，正是它标记着作业
    从未被认领，而 `claim_job` 读它来决定下一次走查是首次尝试还是重试。
    """
    validate_target_pages(target_pages)
    validate_per_page(per_page)
    created = now or datetime.now(UTC)
    job = BackfillJob(
        provider=provider,
        query_json=dict(query),
        status=PENDING,
        next_page=1,
        target_pages=target_pages,
        per_page=per_page,
        pages_completed=0,
        items_collected=0,
        attempt=1,
        started_at=None,
        finished_at=None,
        heartbeat_at=None,
        created_at=created,
        updated_at=created,
    )
    session.add(job)
    session.commit()
    return job


def get_job(session: Session, job_id: int) -> BackfillJob | None:
    """按主键返回一个作业。"""
    return session.get(BackfillJob, job_id)


def require_job(session: Session, job_id: int) -> BackfillJob:
    """返回一个作业或抛异常，这样调用方不必处理空游标。"""
    job = get_job(session, job_id)
    if job is None:
        raise BackfillJobNotFound(f"backfill job {job_id} does not exist")
    return job


def list_jobs(
    session: Session,
    *,
    provider: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> Sequence[BackfillJob]:
    """返回最近的作业，最新的在前，可按可选条件过滤。"""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    statement = select(BackfillJob)
    if provider is not None:
        statement = statement.where(BackfillJob.provider == provider)
    if status is not None:
        if status not in JOB_STATUSES:
            raise ValueError(f"unsupported job status: {status}")
        statement = statement.where(BackfillJob.status == status)
    statement = statement.order_by(BackfillJob.id.desc()).limit(limit)
    return session.scalars(statement).all()


def claim_job(
    session: Session,
    job: BackfillJob,
    *,
    now: datetime | None = None,
    stale_after: timedelta = STALE_AFTER,
) -> BackfillJob:
    """接管一个作业的游标，或因另一个走查仍持有它而拒绝。

    一个 `running` 状态的作业，只有在心跳早于 `stale_after` 之后才重新
    可认领。心跳是 `heartbeat_at`，每次提交一页都会刷新它，于是一个活着
    的走查免费地让它保持新鲜。拒绝而不是抢占很重要，因为一个游标上的两
    个走查者不只是重复劳动：它们各自当作自己是唯一的那个来推进同一个计
    数器，于是较慢者所在的那几页被计一次、被请求两次，预算还会花在另一
    个走查者已经越过的页上。

    从未被认领过的作业没有心跳，按定义可认领；一个 `running` 却没有心
    跳的作业，是首页从未提交成功的那一种，接管它同样安全。
    """
    moment = now or datetime.now(UTC)
    heartbeat = job.heartbeat_at
    if job.status == RUNNING and heartbeat is not None and moment - heartbeat < stale_after:
        raise BackfillJobBusy(
            f"backfill job {job.id} is still being walked by another process",
            job_id=job.id,
            held_since=heartbeat.isoformat(),
        )
    if job.status == SUCCEEDED:
        raise BackfillNotResumable(f"backfill job {job.id} has already completed its pages")
    if job.next_page > job.target_pages:
        raise BackfillNotResumable(
            f"backfill job {job.id} has no pages left to request: "
            f"next_page={job.next_page}, target_pages={job.target_pages}"
        )
    if job.started_at is None:
        job.started_at = moment
    else:
        job.attempt += 1
    job.status = RUNNING
    job.heartbeat_at = moment
    job.error_code = None
    job.error_message = None
    session.commit()
    return job


def advance_job(
    session: Session,
    job: BackfillJob,
    *,
    page: int,
    item_count: int,
    now: datetime | None = None,
) -> BackfillJob:
    """提交一个已完成的页，并把游标推过它。

    调用方在紧邻本次调用之前、在同一个事务里提交该页的仓库，好让游标和
    它所对应的那些行一起可见。
    """
    if page != job.next_page:
        raise BackfillNotResumable(
            f"backfill job {job.id} expected page {job.next_page}, got {page}"
        )
    job.pages_completed += 1
    job.items_collected += item_count
    job.next_page = page + 1
    job.heartbeat_at = now or datetime.now(UTC)
    session.commit()
    return job


def finish_job(
    session: Session,
    job: BackfillJob,
    *,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
    now: datetime | None = None,
) -> BackfillJob:
    """用终态状态与可选的、已脱敏的错误关闭一个作业。"""
    if status not in TERMINAL_JOB_STATUSES:
        raise ValueError(f"{status} is not a terminal job status")
    moment = now or datetime.now(UTC)
    job.status = status
    job.finished_at = moment
    if job.started_at is None:
        job.started_at = moment
    if error_code is not None:
        job.error_code = error_code[:64]
    if error_message is not None:
        job.error_message = redact_text(error_message)[:MAX_ERROR_MESSAGE_LENGTH]
    session.commit()
    return job


def cancel_job(
    session: Session,
    job: BackfillJob,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> BackfillJob:
    """停下一个作业而不丢弃它的游标，以便之后续跑。"""
    if job.status == SUCCEEDED:
        raise BackfillNotResumable(f"backfill job {job.id} already completed its pages")
    return finish_job(
        session,
        job,
        status=CANCELLED,
        error_code="BACKFILL_CANCELLED",
        error_message=reason,
        now=now,
    )


def summarize_job(job: BackfillJob) -> BackfillJobSummary:
    """把作业行转换成它的可序列化摘要。"""
    return BackfillJobSummary(
        job_id=job.id,
        provider=job.provider,
        status=job.status,
        attempt=job.attempt,
        query=dict(job.query_json or {}),
        next_page=job.next_page,
        target_pages=job.target_pages,
        per_page=job.per_page,
        pages_completed=job.pages_completed,
        items_collected=job.items_collected,
        started_at=job.started_at.isoformat() if job.started_at is not None else None,
        finished_at=job.finished_at.isoformat() if job.finished_at is not None else None,
        heartbeat_at=job.heartbeat_at.isoformat() if job.heartbeat_at is not None else None,
        error_code=job.error_code,
        error_message=job.error_message,
    )


def summarize_jobs(jobs: Sequence[BackfillJob]) -> list[dict[str, Any]]:
    """把作业行转换成可 JSON 序列化的载荷。"""
    return [summarize_job(job).to_dict() for job in jobs]
