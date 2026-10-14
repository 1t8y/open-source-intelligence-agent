"""Tests for resumable backfill jobs and the walk that advances them."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from open_source_intelligence import backfill as backfill_package
from open_source_intelligence.backfill import service as backfill_service
from open_source_intelligence.backfill.errors import (
    BackfillJobBusy,
    BackfillLimitError,
    BackfillNotResumable,
    BackfillUnsupportedCollector,
    BackfillWalkFailed,
)
from open_source_intelligence.backfill.jobs import (
    MAX_TARGET_PAGES,
    STALE_AFTER,
    advance_job,
    cancel_job,
    claim_job,
    create_job,
    finish_job,
    list_jobs,
    require_job,
    summarize_job,
)
from open_source_intelligence.backfill.service import resume_backfill, start_backfill
from open_source_intelligence.collectors.limiter import RateLimiter
from open_source_intelligence.collectors.types import (
    CollectedPage,
    CollectedRepository,
    SourceUnavailableError,
)
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.intelligence import PipelineRun, Repository

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
TOTAL_PAGES = 7


class FakeClock:
    """A clock that advances only when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 500.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class PagedStub:
    """A paged source with a fixed number of pages that can fail on one of them."""

    provider = "github"
    display_name = "GitHub"
    base_url = "https://api.github.com"

    def __init__(self, *, fail_on: int | None = None, total_pages: int = TOTAL_PAGES) -> None:
        self.fail_on = fail_on
        self.total_pages = total_pages
        self.requests: list[int] = []

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        return list(self.collect_page(page=1, per_page=limit).items)

    def collect_page(self, *, page: int, per_page: int) -> CollectedPage:
        self.requests.append(page)
        if page == self.fail_on:
            raise SourceUnavailableError("source returned HTTP 503")
        return CollectedPage(
            page=page,
            items=tuple(
                CollectedRepository(
                    provider=self.provider,
                    provider_repo_id=f"{page}-{index}",
                    full_name=f"org/repo-{page}-{index}",
                    html_url=f"https://github.com/org/repo-{page}-{index}",
                    description=None,
                    language="Python",
                    license="MIT",
                    topics=(),
                    stars=1000 - page,
                    forks=10,
                    observed_at=NOW,
                )
                for index in range(per_page)
            ),
            has_more=page < self.total_pages,
        )

    def describe_query(self) -> dict[str, object]:
        return {"q": "topic:llm stars:>100"}


class OneShotStub:
    """A collector that answers `collect` but cannot be walked."""

    provider = "github"
    display_name = "GitHub"
    base_url = "https://api.github.com"

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        return []


def build_pacer() -> tuple[FakeClock, RateLimiter]:
    """Build a limiter pacing on a fake clock, at two seconds per request."""
    clock = FakeClock()
    return clock, RateLimiter(per_minute=30, clock=clock.time, sleep=clock.sleep)


def repository_count(session: Session) -> int:
    """Return how many repositories the walk has ingested."""
    return session.scalar(select(func.count()).select_from(Repository)) or 0


def backfill_runs(session: Session) -> list[PipelineRun]:
    """Return the run rows the walk wrote, oldest first."""
    return list(
        session.scalars(
            select(PipelineRun).where(PipelineRun.run_type == "backfill").order_by(PipelineRun.id)
        ).all()
    )


# ------------------------------------------------------------------------ job lifecycle


def test_a_new_job_starts_before_its_first_page(session: Session) -> None:
    """The cursor begins at page 1 and nothing has been ingested."""
    job = create_job(
        session, provider="github", query={"q": "llm"}, target_pages=4, per_page=10, now=NOW
    )

    assert job.status == "pending"
    assert job.next_page == 1
    assert job.pages_completed == 0
    assert job.items_collected == 0
    assert job.attempt == 1
    assert job.started_at is None


@pytest.mark.parametrize(
    ("target_pages", "per_page"),
    [(0, 10), (MAX_TARGET_PAGES + 1, 10), (5, 0), (5, 101)],
)
def test_a_job_outside_the_bounds_is_refused(
    session: Session, target_pages: int, per_page: int
) -> None:
    """The application refuses what the CHECK constraints would reject."""
    with pytest.raises(BackfillLimitError):
        create_job(
            session,
            provider="github",
            query={},
            target_pages=target_pages,
            per_page=per_page,
            now=NOW,
        )


def test_the_first_claim_keeps_attempt_one_and_records_when_it_started(session: Session) -> None:
    """`attempt` counts from one, matching the run ledger's own convention."""
    job = create_job(session, provider="github", query={}, now=NOW)

    claim_job(session, job, now=NOW)

    assert job.attempt == 1
    assert job.status == "running"
    assert job.started_at == NOW
    assert job.heartbeat_at == NOW


def test_a_second_claim_counts_as_a_second_attempt(session: Session) -> None:
    """Resuming a walk after a failure is a new attempt over the same cursor."""
    job = create_job(session, provider="github", query={}, now=NOW)
    claim_job(session, job, now=NOW)
    finish_job(session, job, status="failed", error_code="COLLECTOR_FAILED", now=NOW)

    claim_job(session, job, now=NOW + timedelta(minutes=1))

    assert job.attempt == 2
    assert job.error_code is None


def test_claiming_a_job_with_a_live_heartbeat_is_refused(session: Session) -> None:
    """A running job belongs to the walk that holds it."""
    job = create_job(session, provider="github", query={}, now=NOW)
    claim_job(session, job, now=NOW)

    with pytest.raises(BackfillJobBusy) as caught:
        claim_job(session, job, now=NOW + timedelta(minutes=1))

    assert caught.value.job_id == job.id


def test_a_stale_heartbeat_lets_another_walk_take_over(session: Session) -> None:
    """A walk whose process died must not strand the job forever."""
    job = create_job(session, provider="github", query={}, now=NOW)
    claim_job(session, job, now=NOW)

    claim_job(session, job, now=NOW + STALE_AFTER + timedelta(seconds=1))

    assert job.status == "running"
    assert job.attempt == 2
    assert job.heartbeat_at == NOW + STALE_AFTER + timedelta(seconds=1)


def test_the_heartbeat_tracks_the_last_page_that_committed(session: Session) -> None:
    """A live walk keeps its claim fresh for free, one page at a time."""
    job = create_job(session, provider="github", query={}, target_pages=3, now=NOW)
    claim_job(session, job, now=NOW)

    advance_job(session, job, page=1, item_count=2, now=NOW + timedelta(seconds=30))

    assert job.heartbeat_at == NOW + timedelta(seconds=30)
    assert job.next_page == 2


def test_claiming_a_finished_job_is_refused(session: Session) -> None:
    """Nothing is left to walk once every page landed."""
    job = create_job(session, provider="github", query={}, target_pages=1, now=NOW)
    claim_job(session, job, now=NOW)
    advance_job(session, job, page=1, item_count=3, now=NOW)
    finish_job(session, job, status="succeeded", now=NOW)

    with pytest.raises(BackfillNotResumable):
        claim_job(session, job, now=NOW + timedelta(hours=1))


def test_advancing_out_of_order_is_refused(session: Session) -> None:
    """A cursor that skipped a page would silently drop that page's rows."""
    job = create_job(session, provider="github", query={}, now=NOW)
    claim_job(session, job, now=NOW)

    with pytest.raises(BackfillNotResumable, match="expected page 1"):
        advance_job(session, job, page=4, item_count=3, now=NOW)


def test_cancelling_keeps_the_cursor_so_the_job_stays_resumable(session: Session) -> None:
    """A cancelled job is a paused one, not a discarded one."""
    job = create_job(session, provider="github", query={}, target_pages=5, now=NOW)
    claim_job(session, job, now=NOW)
    advance_job(session, job, page=1, item_count=3, now=NOW)

    cancel_job(session, job, reason="operator stopped it", now=NOW)

    assert job.status == "cancelled"
    assert job.next_page == 2
    assert job.pages_completed == 1
    assert summarize_job(job).resumable is True
    claim_job(session, job, now=NOW + timedelta(minutes=1))
    assert job.attempt == 2


def test_the_summary_reports_what_a_resume_command_would_need(session: Session) -> None:
    """`pages_remaining` and `resumable` are the two facts an operator acts on."""
    job = create_job(session, provider="github", query={}, target_pages=4, now=NOW)
    claim_job(session, job, now=NOW)
    advance_job(session, job, page=1, item_count=2, now=NOW)
    finish_job(session, job, status="failed", error_code="COLLECTOR_FAILED", now=NOW)

    summary = summarize_job(job).to_dict()

    assert summary["pages_remaining"] == 3
    assert summary["resumable"] is True
    assert summary["error_code"] == "COLLECTOR_FAILED"


def test_the_job_list_filters_by_provider_and_status(session: Session) -> None:
    """Operators look for the failed walk, not for every walk."""
    create_job(session, provider="github", query={}, now=NOW)
    other = create_job(session, provider="huggingface", query={}, now=NOW)
    claim_job(session, other, now=NOW)
    finish_job(session, other, status="failed", error_code="COLLECTOR_FAILED", now=NOW)

    assert len(list_jobs(session, provider="github")) == 1
    assert [job.provider for job in list_jobs(session, status="failed")] == ["huggingface"]
    with pytest.raises(ValueError, match="unsupported job status"):
        list_jobs(session, status="nonsense")


# ------------------------------------------------------------------------- walking


def test_a_walk_ingests_every_page_and_closes_the_job(session: Session) -> None:
    """The happy path: the budget is spent, the cursor ends past it, the job succeeds."""
    _, limiter = build_pacer()
    stub = PagedStub()

    outcome = start_backfill(
        session, collector=stub, target_pages=5, per_page=4, limiter=limiter, now=NOW
    )

    assert outcome.status == "succeeded"
    assert outcome.pages_this_attempt == 5
    assert outcome.items_this_attempt == 20
    assert outcome.pages_completed == 5
    assert outcome.next_page == 6
    assert outcome.exhausted is False
    assert stub.requests == [1, 2, 3, 4, 5]
    assert repository_count(session) == 20


def test_an_interrupted_walk_resumes_where_it_stopped(session: Session) -> None:
    """The headline guarantee: the cursor survives the failure that stopped the walk.

    The first walk dies on page 3. Everything it had already ingested stays, the cursor
    points at the page that failed, and the job is still resumable - so the second walk
    starts at page 3 rather than at page 1.
    """
    _, limiter = build_pacer()
    failing = PagedStub(fail_on=3)

    with pytest.raises(BackfillWalkFailed) as caught:
        start_backfill(
            session, collector=failing, target_pages=5, per_page=4, limiter=limiter, now=NOW
        )

    assert caught.value.error_code == "COLLECTOR_FAILED"
    assert caught.value.next_page == 3
    interrupted = require_job(session, caught.value.job_id)
    assert interrupted.status == "failed"
    assert interrupted.pages_completed == 2
    assert interrupted.items_collected == 8
    assert interrupted.next_page == 3
    assert repository_count(session) == 8
    assert failing.requests == [1, 2, 3]

    resumed_stub = PagedStub()
    outcome = resume_backfill(
        session, job_id=interrupted.id, collector=resumed_stub, limiter=limiter, now=NOW
    )

    assert resumed_stub.requests == [3, 4, 5], "the resumed walk re-read pages it had passed"
    assert outcome.started_page == 3
    assert outcome.attempt == 2
    assert outcome.pages_completed == 5
    assert outcome.items_collected == 20
    assert outcome.status == "succeeded"
    assert repository_count(session) == 20


def test_a_walk_records_one_run_row_per_attempt_linked_to_its_job(session: Session) -> None:
    """Each attempt is a separate run, so the retry trail stays visible."""
    _, limiter = build_pacer()
    with pytest.raises(BackfillWalkFailed):
        start_backfill(
            session,
            collector=PagedStub(fail_on=2),
            target_pages=3,
            per_page=2,
            limiter=limiter,
            now=NOW,
        )
    resume_backfill(session, job_id=1, collector=PagedStub(), limiter=limiter, now=NOW)

    runs = backfill_runs(session)

    assert [(run.status, run.attempt) for run in runs] == [("failed", 1), ("succeeded", 2)]
    assert [run.error_code for run in runs] == ["COLLECTOR_FAILED", None]
    assert [run.topic_id for run in runs] == [None, None]
    assert runs[1].metrics_json is not None
    assert runs[1].metrics_json["backfill_job_id"] == 1
    assert runs[1].metrics_json["pages_this_attempt"] == 2
    assert runs[1].metrics_json["exhausted"] is False


def test_a_failed_page_is_recorded_on_the_job_and_on_the_run(session: Session) -> None:
    """Both records agree, so neither has to be read to interpret the other."""
    _, limiter = build_pacer()

    with pytest.raises(BackfillWalkFailed):
        start_backfill(
            session,
            collector=PagedStub(fail_on=1),
            target_pages=2,
            per_page=2,
            limiter=limiter,
            now=NOW,
        )

    job = require_job(session, 1)
    run = backfill_runs(session)[0]
    assert job.error_code == run.error_code == "COLLECTOR_FAILED"
    assert job.status == run.status == "failed"
    assert "503" in (job.error_message or "")


def test_the_failure_is_audited_as_well_as_recorded(session: Session) -> None:
    """A walk that failed must leave an audit trail, not only a run row."""
    _, limiter = build_pacer()
    with pytest.raises(BackfillWalkFailed):
        start_backfill(
            session,
            collector=PagedStub(fail_on=1),
            target_pages=2,
            per_page=2,
            limiter=limiter,
            now=NOW,
        )

    event = session.scalars(select(AuditLog).where(AuditLog.action == "backfill.run")).one()

    assert event.result == "failed"
    assert event.resource_type == "backfill_job"
    assert event.resource_id == "1"


def test_a_successful_walk_is_audited_once(session: Session) -> None:
    """The success path audits the walk, not each page."""
    _, limiter = build_pacer()

    start_backfill(
        session, collector=PagedStub(), target_pages=4, per_page=3, limiter=limiter, now=NOW
    )

    events = session.scalars(select(AuditLog).where(AuditLog.action == "backfill.run")).all()

    assert len(events) == 1
    assert events[0].result == "success"
    assert events[0].metadata_json is not None
    assert events[0].metadata_json["pages_this_attempt"] == 4


def test_a_source_that_runs_out_early_closes_the_job_as_exhausted(session: Session) -> None:
    """A budget larger than the history is not an error; the history is simply shorter."""
    _, limiter = build_pacer()

    outcome = start_backfill(
        session, collector=PagedStub(total_pages=3), target_pages=50, per_page=2, limiter=limiter
    )

    assert outcome.exhausted is True
    assert outcome.pages_this_attempt == 3
    assert outcome.next_page == 4
    assert outcome.status == "succeeded"
    assert require_job(session, outcome.job_id).status == "succeeded"


def test_a_single_page_source_stops_after_one_request(session: Session) -> None:
    """`has_more` false on the first page means there is no second request to spend."""
    _, limiter = build_pacer()
    stub = PagedStub(total_pages=1)

    outcome = start_backfill(
        session, collector=stub, target_pages=10, per_page=3, limiter=limiter, now=NOW
    )

    assert stub.requests == [1]
    assert outcome.exhausted is True
    assert outcome.pages_this_attempt == 1


# ----------------------------------------------------------------------- pacing


def test_a_walk_paces_exactly_one_interval_between_pages(session: Session) -> None:
    """Five pages cost four intervals, and the walk reports them."""
    clock, limiter = build_pacer()

    outcome = start_backfill(
        session, collector=PagedStub(), target_pages=5, per_page=2, limiter=limiter, now=NOW
    )

    assert clock.slept == [limiter.interval_seconds] * 4
    assert outcome.paced_seconds == pytest.approx(4 * limiter.interval_seconds)


def test_reported_pacing_counts_only_this_walk(session: Session) -> None:
    """A limiter reused across a resume must not credit the second walk with the first's wait.

    The outcome describes one attempt. Reading the limiter's running total would have the
    resumed walk claim every second the interrupted one spent being polite to the source.
    """
    _, limiter = build_pacer()
    with pytest.raises(BackfillWalkFailed):
        start_backfill(
            session,
            collector=PagedStub(fail_on=3),
            target_pages=5,
            per_page=2,
            limiter=limiter,
            now=NOW,
        )
    before = limiter.total_waited_seconds

    outcome = resume_backfill(session, job_id=1, collector=PagedStub(), limiter=limiter, now=NOW)

    assert before > 0
    assert outcome.paced_seconds == pytest.approx(limiter.total_waited_seconds - before)
    assert outcome.paced_seconds < limiter.total_waited_seconds


def test_the_walk_defaults_to_the_budget_configured_on_the_source(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rate limit in the data source row is the one the walk enforces."""
    captured: list[int] = []
    real = RateLimiter

    def spy(*, per_minute: int) -> RateLimiter:
        captured.append(per_minute)
        clock = FakeClock()
        return real(per_minute=per_minute, clock=clock.time, sleep=clock.sleep)

    monkeypatch.setattr(backfill_service, "RateLimiter", spy)

    start_backfill(session, collector=PagedStub(), target_pages=1, per_page=2, now=NOW)

    assert captured == [30], "the source's own rate_limit_per_minute must be the default"


# ------------------------------------------------------------------ refusals and edges


def test_a_collector_that_cannot_page_is_refused(session: Session) -> None:
    """A one-shot source cannot be walked, and saying so beats paging it once."""
    with pytest.raises(BackfillUnsupportedCollector, match="cannot be walked"):
        start_backfill(session, collector=OneShotStub(), target_pages=2, per_page=2, now=NOW)


def test_walking_the_same_pages_twice_does_not_duplicate_repositories(session: Session) -> None:
    """Ingestion stays idempotent on (source, provider_repo_id), so a replayed page is free.

    This is what makes the crash window between a page's rows and its cursor harmless: even a
    second job that walks the same history updates the same rows rather than adding a copy.
    """
    _, limiter = build_pacer()
    stub = PagedStub()
    start_backfill(session, collector=stub, target_pages=2, per_page=3, limiter=limiter, now=NOW)
    after_first = repository_count(session)

    second = start_backfill(
        session, collector=stub, target_pages=2, per_page=3, limiter=limiter, now=NOW
    )

    assert after_first == 6
    assert second.items_this_attempt == 6
    assert repository_count(session) == 6


def test_a_walk_that_has_budget_left_but_no_pages_is_refused_up_front(session: Session) -> None:
    """Better a clear refusal than a walk that immediately reports success doing nothing."""
    job = create_job(session, provider="github", query={}, target_pages=2, now=NOW)
    claim_job(session, job, now=NOW)
    advance_job(session, job, page=1, item_count=1, now=NOW)
    advance_job(session, job, page=2, item_count=1, now=NOW)
    finish_job(session, job, status="failed", error_code="COLLECTOR_FAILED", now=NOW)

    with pytest.raises(BackfillNotResumable, match="no pages left"):
        resume_backfill(session, job_id=job.id, collector=PagedStub(), now=NOW)


def test_the_package_exports_the_public_surface() -> None:
    """The module's `__all__` is the contract other layers import against."""
    for name in (
        "BackfillJobBusy",
        "BackfillJobNotFound",
        "BackfillLimitError",
        "BackfillNotResumable",
        "BackfillOutcome",
        "BackfillUnsupportedCollector",
        "BackfillWalkFailed",
        "create_job",
        "require_job",
        "resume_backfill",
        "start_backfill",
    ):
        assert hasattr(backfill_package, name)
