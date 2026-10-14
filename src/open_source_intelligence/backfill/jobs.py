"""Backfill job lifecycle and cursor persistence.

The job row *is* the checkpoint. Everything a resume needs - which page to ask for next and
how much has already been ingested - lives in columns rather than in the process doing the
walking, because the process is the part that dies.

All timestamps here are timezone-aware UTC. The column type refuses naive values on write,
so a caller that pins the clock must pin it to an aware instant rather than a bare one.

One trap worth stating once: `TimestampMixin.updated_at` cannot be pinned. Its Python-side
`onupdate` is applied by SQLAlchemy on every UPDATE and wins over an explicit assignment, so
the column always ends up holding the wall clock. That is why the liveness signal below is
`heartbeat_at` rather than `updated_at` - a guard whose clock a test cannot control is a
guard nobody will test.
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
"""Ceiling on a walk's page budget.

GitHub's search API refuses to page past its 1000-result cap, so a larger budget would only
buy requests the source will not serve. Keeping the budget inside the same range as
`MAX_PER_PAGE` also means `next_page` can never name a page no provider will answer.
"""

STALE_AFTER: Final[timedelta] = timedelta(minutes=15)
"""How long a running job may go without a heartbeat before another walk may claim it."""

MAX_ERROR_MESSAGE_LENGTH: Final[int] = 2000


@dataclass(frozen=True, slots=True)
class BackfillJobSummary:
    """Serializable view of one backfill job."""

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
        """Return how many pages the walk still intends to request."""
        return max(self.target_pages - self.pages_completed, 0)

    @property
    def resumable(self) -> bool:
        """Return whether another walk would make progress."""
        return self.status != SUCCEEDED and self.next_page <= self.target_pages

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready payload for CLI and worker output."""
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
    """Reject a page budget outside what the schema accepts."""
    if not 1 <= target_pages <= MAX_TARGET_PAGES:
        raise BackfillLimitError(f"target_pages must be between 1 and {MAX_TARGET_PAGES}")
    return target_pages


def validate_per_page(per_page: int) -> int:
    """Reject a page size outside what the schema and the providers accept."""
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
    """Create a pending job positioned before its first page.

    `attempt` starts at 1 even though nothing has been walked yet, because the ledger's own
    convention is that an attempt counts from one and never from zero. `started_at` staying
    null is what marks the job as never claimed, and `claim_job` reads that to decide
    whether the next walk is the first attempt or a repeat.
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
    """Return one job by primary key."""
    return session.get(BackfillJob, job_id)


def require_job(session: Session, job_id: int) -> BackfillJob:
    """Return one job or raise, so callers do not have to handle a null cursor."""
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
    """Return recent jobs, newest first, filtered by optional criteria."""
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
    """Take ownership of a job's cursor, or refuse because another walk holds it.

    A `running` job becomes claimable again only once its heartbeat is older than
    `stale_after`. The heartbeat is `heartbeat_at`, which every page commit refreshes, so a
    live walk keeps it fresh for free. Refusing rather than stealing matters because two
    walkers on one cursor do not merely duplicate work: each advances the same counter as if
    it were alone, so the pages the slower one is on get counted once and requested twice,
    and the budget can be spent on pages the other walker has already passed.

    A job that has never been claimed has no heartbeat and is claimable by definition; a
    `running` job with no heartbeat is one whose first page never committed, which is also
    safe to take over.
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
    """Commit one finished page and move the cursor past it.

    Callers commit the page's repositories in the same transaction, immediately before
    this call, so that the cursor and the rows it accounts for become visible together.
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
    """Close a job with a terminal status and an optional redacted error."""
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
    """Stop a job without discarding its cursor, so it can be resumed later."""
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
    """Convert a job row into its serializable summary."""
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
    """Convert job rows into JSON-ready payloads."""
    return [summarize_job(job).to_dict() for job in jobs]
