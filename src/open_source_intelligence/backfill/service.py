"""The backfill walk: page by page, cursor committed after each one.

Two properties shape everything below. The first is that a walk is *resumable*: the cursor
is persisted per page, so an interruption costs the page in flight rather than the whole
history. The second is that a walk is *paced*: a backfill is the one part of this system
that asks a public API the same question fifty times in a row, so the request budget the
data source row has carried since T4 finally gets spent.
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
    """What one page of the walk produced."""

    page: int
    item_count: int
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready payload."""
        return {"page": self.page, "item_count": self.item_count, "has_more": self.has_more}


@dataclass(frozen=True, slots=True)
class BackfillOutcome:
    """Aggregated result of one backfill walk."""

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
        """Return a JSON-ready payload for CLI, worker, and log output."""
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
    """Return the collector as a paging collector, or explain that it cannot page."""
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
    """Walk a job's remaining pages, committing the cursor after each one.

    The cursor is committed per page rather than at the end, which is the entire point: a
    walk killed after page 7 of 20 leaves a job whose `next_page` is 8, and the next run
    starts there. Committing once on success would make every interruption a full replay.

    A page's repositories and the cursor that points past them are committed in the same
    transaction. Ingestion is idempotent on (source, provider_repo_id), so a replay could
    not duplicate a row anyway - but this makes the crash window harmless rather than merely
    survivable, and it means a cursor that moved is always a page that landed.

    `limiter` defaults to one sized from the data source's own `rate_limit_per_minute`, so
    the budget configured on the source row is the budget actually enforced. Passing one in
    is how tests pace with an injected clock instead of real sleeps. `now` pins only the
    boundaries - the claim and the close - while per-page heartbeats still come from the
    wall clock, because a heartbeat that a caller froze would look like a dead walker.

    Raises:
        BackfillUnsupportedCollector: when the source cannot be paged.
        BackfillJobBusy: when another walk still owns the cursor.
        BackfillNotResumable: when the job has no pages left.
        BackfillWalkFailed: when a page fails. The job and run rows already record the
            failure and the page the cursor stopped on.
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
    # Measured as a delta rather than read off the limiter directly: a limiter may outlive
    # one walk (a resume reusing the same object is the obvious case), and an outcome that
    # reported the running total would credit this walk with the waiting a previous one did.
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
        # The audit row rides along in the run's commit, so a failed walk leaves both
        # records without a second transaction that could itself fail.
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
    """Create a job for a source and walk it in one call."""
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
    """Resume a job from the cursor it stopped at."""
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
    """Describe one walk's progress for the run ledger."""
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
