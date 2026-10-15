"""Run T16 backfill and end-to-end weekly report smoke checks against MySQL.

T16's acceptance criterion is one sentence: *complete a historical backfill and a full
weekly report*. This script performs both on a real database through the real services -
nothing here is mocked except the far side of the socket - and it does them in the order the
criterion implies, because that order is itself the point:

1. **Walk a history first.** The real `GitHubCollector` is pointed at an `httpx.MockTransport`
   shaped exactly like GitHub's search API, and the real `run_backfill` walks it. Weaving the
   mock into the *transport* rather than into the collector is deliberate: the adapter still
   builds the URL, still reads `has_more` out of the `Link` header, still normalizes the items,
   and `RetryingHttpClient` still retries. Only the answers are synthetic.

2. **Then a weekly report.** The real `run_daily_pipeline` runs collect -> snapshot -> rank over
   the repositories the walk just landed, the real Agent produces a draft, the real composition
   promotes it to a versioned report, the real verifier grades it, and the real publishing
   state machine takes it from `draft` to `published` through an actual human review.

What the smoke is designed to catch, in each case because the naive implementation is wrong:

* **A page budget is not a page count.** The mock advertises `total_count=4210` while refusing
  to serve past page 4, which is precisely what GitHub does once a search walks into its
  1000-result cap. A walker that derived its last page from `total_count` would request pages
  the source will never answer; this one has to stop on the absent `rel="next"`.
* **An interruption costs the page in flight, not the history.** Page 3 is made to fail, the
  walk is required to record `next_page=3`, and the resume is required to request pages 3 and 4
  *and nothing else*. Re-reading pages 1 and 2 would prove the cursor was decoration.
* **The budget configured on the source row is the budget enforced.** One walk deliberately
  passes no limiter and has its limiter construction recorded, so the number the walk spent can
  be compared with the number the data source carries.
* **A crash window that is harmless, not merely survivable.** Ingestion is idempotent on
  `(source, provider_repo_id)`, so replaying a walk leaves the repository count exactly where it
  was. The check asserts the *count*, because that is the only thing a duplicate would change.
* **"The source refused" and "our own write broke" stay distinguishable.** A failed walk has to
  land on `COLLECTOR_FAILED`, not on the catch-all `PIPELINE_UNEXPECTED_ERROR`.

Every database read the script asserts on goes through a *fresh* session at the end, so a
passing assertion cannot be explained by the identity map of the session that wrote the row.
The default `agent_provider="offline"` model client is used throughout: it is the deployment
default, it needs no key, and being a pure function of the conversation it keeps the run
reproducible.
"""

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

import open_source_intelligence.backfill.service as backfill_service
from open_source_intelligence.agent.runtime import DEFAULT_AGENT_CONFIG_NAME, run_research_agent
from open_source_intelligence.backfill.errors import (
    BackfillJobBusy,
    BackfillLimitError,
    BackfillNotResumable,
    BackfillUnsupportedCollector,
    BackfillWalkFailed,
)
from open_source_intelligence.backfill.jobs import (
    FAILED,
    STALE_AFTER,
    SUCCEEDED,
    claim_job,
    create_job,
    list_jobs,
    summarize_job,
)
from open_source_intelligence.backfill.service import (
    resume_backfill,
    run_backfill,
    start_backfill,
)
from open_source_intelligence.collectors.github import GitHubCollector
from open_source_intelligence.collectors.http import RetryingHttpClient
from open_source_intelligence.collectors.limiter import RateLimiter
from open_source_intelligence.collectors.service import ensure_data_source
from open_source_intelligence.collectors.types import CollectedRepository
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.models.agent import (
    AgentConfig,
    AgentRun,
    AgentStep,
    ToolCall,
)
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import (
    BackfillJob,
    PipelineRun,
    Repository,
    RepositoryContent,
    RepoSnapshot,
    Topic,
    TopicRepository,
)
from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportDraft,
    ReportReview,
    ReportSection,
    ReportSource,
    ReportVerification,
    ReportVersion,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.pipeline.daily import run_daily_pipeline
from open_source_intelligence.reports.composition import generate_report
from open_source_intelligence.reports.publishing import (
    publish_due_reports,
    publish_or_request_review,
    record_review,
)
from open_source_intelligence.reports.verification import verify_report

# --------------------------------------------------------------------------------------
# Smoke-owned names, dates, and clocks
# --------------------------------------------------------------------------------------

TOPIC_NAME = "__T16 Weekly Topic__"
KEYWORD = "__t16_weekly__"
SMOKE_QUERY = "__t16_smoke__ stars:>0"
REPO_PREFIX = "__t16_smoke_"
VERIFIER_USERNAME = "__t16_verifier__"

#: The walk clock. A synthetic instant, so a resume is obviously a resume and not a race.
WALK_NOW = datetime(2099, 6, 6, 9, 0, tzinfo=UTC)
#: The business day the weekly report covers. Deliberately far in the future: the smoke's
#: snapshot stage touches *every* repository, so its rows have to carry a date no real run can
#: ever have written, or cleaning up after the smoke would delete somebody's history.
BUSINESS_NOW = datetime(2099, 6, 7, 12, 0, tzinfo=UTC)
BUSINESS_DATE = date(2099, 6, 7)
SMOKE_SNAPSHOT_DATES = (BUSINESS_DATE,)

#: How many repositories the topic hands the Agent. Two, not ten. The deterministic offline
#: client researches *every* candidate it is shown - a profile and a metric series each, plus one
#: prior-report query and up to two README reviews - which is `2n + 3` research tool calls against
#: a budget of `agent_max_tool_calls = 10`. At the default five candidates that is thirteen calls:
#: the walk is cut off part-way, `budget_degraded` goes true and the evidence check can only
#: report itself incomplete, so the weekly report never survives verification. Two fits with room
#: to spare and is the same shape the T8 research smoke already proves end to end.
AGENT_CANDIDATES = 2

#: What the mock endpoint claims to hold. Roughly 1400 pages at three per page - far past the
#: four it will actually serve, which is the point.
MOCK_TOTAL_COUNT = 4210
MOCK_PAGE_COUNT = 4
MOCK_PER_PAGE = 3
FAILING_PAGE = 3
DAILY_LIMIT = 4

#: One interval at 60 requests per minute. The paces below are asserted in whole intervals
#: rather than in seconds so the arithmetic stays readable.
PER_MINUTE = 60
INTERVAL_SECONDS = 60.0 / PER_MINUTE

#: Every mock endpoint built during the run, so the transports can be closed deterministically.
_MOCKS: list["MockGitHub"] = []


class FakeClock:
    """A monotonic clock whose sleeping is instant and recorded.

    A pace proven by really sleeping is a pace nobody keeps asserting, so the limiter is given
    a clock and a sleeper instead of `time`. The recorded sleeps are then the assertion: the
    walk had to spend exactly one interval between consecutive requests and nothing more.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        """Return the current fake instant."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Record a sleep and advance the fake instant by it."""
        self.sleeps.append(seconds)
        self.now += seconds

    def jump(self, seconds: float) -> None:
        """Advance the fake instant without recording a sleep."""
        self.now += seconds


class MockGitHub:
    """The real GitHub adapter, answering from a GitHub-shaped mock transport.

    The mock decides what the *server* said; it does not decide how the client behaves. The URL
    is built by `GitHubCollector`, `has_more` is read out of a real `Link` header by
    `parse_link_header`, the retry policy is `RetryingHttpClient`'s, and the normalization is
    the adapter's. That is what makes these checks evidence about the shipped code rather than
    about this file.
    """

    def __init__(
        self,
        *,
        label: str,
        page_count: int = MOCK_PAGE_COUNT,
        total_count: int = MOCK_TOTAL_COUNT,
        failing_pages: Sequence[int] = (),
    ) -> None:
        self.label = label
        self.page_count = page_count
        self.total_count = total_count
        self.failing_pages = set(failing_pages)
        self.requests: list[dict[str, Any]] = []
        self._client = httpx.Client(transport=httpx.MockTransport(self._handle), timeout=10)
        self.collector = GitHubCollector(
            RetryingHttpClient(self._client),
            query=SMOKE_QUERY,
        )
        _MOCKS.append(self)

    # -- server side ------------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one search request the way GitHub's search API would."""
        params = dict(request.url.params)
        page = int(params["page"])
        per_page = int(params["per_page"])
        self.requests.append({"page": page, "per_page": per_page, "q": params.get("q", "")})
        if page in self.failing_pages:
            # A 5xx with no `Link` header. The real client retries this `max_attempts` times
            # before it gives up, which is why `requests_for` can count more than one hit.
            return httpx.Response(500, json={"message": "server error"})
        items = [self._item(page, index, per_page) for index in range(per_page)]
        # Only `rel` is ever read from this header, so the targets just have to look like URLs.
        headers: dict[str, str] = {}
        if page < self.page_count:
            headers["Link"] = (
                f'<https://api.github.com/search/repositories?page={page + 1}>; rel="next", '
                f'<https://api.github.com/search/repositories?page={self.page_count}>; rel="last"'
            )
        return httpx.Response(
            200,
            json={
                "total_count": self.total_count,
                "incomplete_results": False,
                "items": items,
            },
            headers=headers,
        )

    def _item(self, page: int, index: int, per_page: int) -> dict[str, Any]:
        """Return one GitHub repository object, numbered by its place in the walk."""
        number = (page - 1) * per_page + index
        return {
            "id": f"{REPO_PREFIX}{self.label}_{number}",
            "full_name": f"__t16_smoke__/{self.label}-{number}",
            "html_url": f"https://example.com/__t16_smoke__/{self.label}-{number}",
            "description": f"T16 smoke repository for {KEYWORD}",
            "language": "Python",
            "license": {"spdx_id": "MIT"},
            "topics": [KEYWORD],
            "stargazers_count": 500 + number,
            "forks_count": 50 + number,
        }

    # -- client-visible helpers -------------------------------------------------------

    def open_page(self, page: int) -> None:
        """Stop failing a page, which is what an operator fixing an outage does."""
        self.failing_pages.discard(page)

    def close(self) -> None:
        """Release the transport the mock owns."""
        self._client.close()

    def pages_requested(self) -> list[int]:
        """Return the page of every request the endpoint received, in order."""
        return [entry["page"] for entry in self.requests]

    def pages_requested_since(self, mark: int) -> list[int]:
        """Return the pages requested since a previously taken mark."""
        return [entry["page"] for entry in self.requests[mark:]]

    def requests_for(self, page: int) -> int:
        """Return how many requests the endpoint received for one page."""
        return sum(1 for entry in self.requests if entry["page"] == page)


class NotPaginated:
    """A collector that can collect, but cannot be walked page by page."""

    provider = "t16_not_paginated"
    display_name = "T16 Not Paginated"
    base_url = "https://example.com"

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        """Collect nothing; this stand-in only exists to be refused."""
        del limit
        return []


# --------------------------------------------------------------------------------------
# Seeding and cleanup
# --------------------------------------------------------------------------------------


def _smoke_job_ids(session: Session) -> list[int]:
    """Return the ids of every backfill job this smoke ever created.

    Scoped by the job's own recorded query rather than by provider: a deployed database holds
    real operator jobs, and a smoke cleanup has no business sweeping those away. The query the
    mock walks is unique to this script, so it is also the safest filter available.
    """
    return [
        job.id
        for job in session.scalars(select(BackfillJob)).all()
        if (job.query_json or {}).get("q") == SMOKE_QUERY
    ]


def _delete_pipeline_runs(session: Session, runs: Sequence[PipelineRun]) -> int:
    """Delete the given pipeline runs and return how many there were."""
    if not runs:
        return 0
    session.execute(delete(PipelineRun).where(PipelineRun.id.in_([run.id for run in runs])))
    return len(runs)


def cleanup_verification_records(session: Session) -> dict[str, Any]:
    """Remove every record created by a previous T16 smoke run.

    Deliberately narrow. Only rows this smoke can prove it owns are removed: repositories are
    matched by provider prefix, snapshots by a date no real run could have used, the topic by
    name, and backfill jobs by the query this script is the only author of. Anything else in
    the database - the 22 repositories a real collection left behind, other smokes' topics -
    is left exactly where it was.
    """
    removed: dict[str, int] = {}

    topic_ids = list(session.scalars(select(Topic.id).where(Topic.name == TOPIC_NAME)).all())
    agent_run_ids: list[int] = []
    if topic_ids:
        run_ids = list(
            session.scalars(select(PipelineRun.id).where(PipelineRun.topic_id.in_(topic_ids))).all()
        )
        if run_ids:
            agent_run_ids = list(
                session.scalars(
                    select(AgentRun.id).where(AgentRun.pipeline_run_id.in_(run_ids))
                ).all()
            )
        agent_run_ids += list(
            session.scalars(select(AgentRun.id).where(AgentRun.topic_id.in_(topic_ids))).all()
        )

    report_ids = (
        list(session.scalars(select(Report.id).where(Report.topic_id.in_(topic_ids))).all())
        if topic_ids
        else []
    )
    if agent_run_ids:
        report_ids += list(
            session.scalars(select(Report.id).where(Report.agent_run_id.in_(agent_run_ids))).all()
        )
    report_ids = sorted(set(report_ids))
    if report_ids:
        session.execute(delete(ReportSource).where(ReportSource.report_id.in_(report_ids)))
        session.execute(delete(ReportReview).where(ReportReview.report_id.in_(report_ids)))
        session.execute(
            delete(ReportVerification).where(ReportVerification.report_id.in_(report_ids))
        )
        session.execute(delete(ReportVersion).where(ReportVersion.report_id.in_(report_ids)))
        session.execute(delete(Report).where(Report.id.in_(report_ids)))
    removed["reports"] = len(report_ids)

    if agent_run_ids:
        draft_ids = select(ReportDraft.id).where(ReportDraft.agent_run_id.in_(agent_run_ids))
        session.execute(delete(ReportSection).where(ReportSection.report_draft_id.in_(draft_ids)))
        session.execute(delete(ReportDraft).where(ReportDraft.agent_run_id.in_(agent_run_ids)))
        session.execute(delete(ToolCall).where(ToolCall.agent_run_id.in_(agent_run_ids)))
        session.execute(delete(AgentStep).where(AgentStep.agent_run_id.in_(agent_run_ids)))
        session.execute(delete(AgentRun).where(AgentRun.id.in_(agent_run_ids)))
    removed["agent_runs"] = len(agent_run_ids)

    job_ids = _smoke_job_ids(session)
    backfill_runs = [
        run
        for run in session.scalars(select(PipelineRun)).all()
        if run.run_type == "backfill"
        or (run.metrics_json or {}).get("backfill_job_id") in job_ids
        or (run.metrics_json or {}).get("business_date") == BUSINESS_DATE.isoformat()
    ]
    if topic_ids:
        backfill_runs += list(
            session.scalars(select(PipelineRun).where(PipelineRun.topic_id.in_(topic_ids))).all()
        )
    removed["pipeline_runs"] = _delete_pipeline_runs(
        session, list({id(run): run for run in backfill_runs}.values())
    )

    if job_ids:
        session.execute(delete(BackfillJob).where(BackfillJob.id.in_(job_ids)))
        # Scoped to this smoke's own jobs. The audit table is append-only by design, and that
        # property is exactly why this delete is so narrow: the rows removed here are ones the
        # smoke itself wrote about jobs it is removing in the same breath.
        session.execute(
            delete(AuditLog).where(
                AuditLog.action == "backfill.run",
                AuditLog.resource_id.in_([str(job_id) for job_id in job_ids]),
            )
        )
    removed["backfill_jobs"] = len(job_ids)

    if topic_ids:
        session.execute(delete(TopicRepository).where(TopicRepository.topic_id.in_(topic_ids)))
        session.execute(delete(Topic).where(Topic.id.in_(topic_ids)))
    removed["topics"] = len(topic_ids)

    snapshots = session.execute(
        delete(RepoSnapshot).where(RepoSnapshot.snapshot_date.in_(SMOKE_SNAPSHOT_DATES))
    )
    removed["repo_snapshots"] = int(snapshots.rowcount or 0)

    repositories = session.scalars(
        select(Repository.id).where(
            Repository.provider_repo_id.startswith(REPO_PREFIX, autoescape=True)
        )
    ).all()
    if repositories:
        session.execute(
            delete(RepositoryContent).where(RepositoryContent.repository_id.in_(repositories))
        )
        session.execute(delete(RepoSnapshot).where(RepoSnapshot.repository_id.in_(repositories)))
        session.execute(delete(Repository).where(Repository.id.in_(repositories)))
    removed["repositories"] = len(repositories)

    # The runtime creates this config on first use; the smokes are its only consumer here.
    session.execute(delete(AgentConfig).where(AgentConfig.name == DEFAULT_AGENT_CONFIG_NAME))
    session.execute(delete(User).where(User.username == VERIFIER_USERNAME))
    session.commit()
    return removed


def seed_topic(session: Session) -> dict[str, int]:
    """Create the accountable user and the topic the weekly report belongs to."""
    user = User(
        username=VERIFIER_USERNAME,
        password_hash="t16-verification-only",
        display_name="T16 Verifier",
        status="active",
        data_scope="team",
    )
    session.add(user)
    session.flush()
    topic = Topic(
        name=TOPIC_NAME,
        description="T16 end-to-end weekly report topic",
        keywords_json=[KEYWORD],
        min_stars=0,
        window_days=7,
        max_candidates=AGENT_CANDIDATES,
        publish_mode="AUTO_WITH_DELAY",
        enabled=True,
        created_by=user.id,
    )
    session.add(topic)
    session.commit()
    return {"user_id": user.id, "topic_id": topic.id}


# --------------------------------------------------------------------------------------
# Part one: the historical backfill
# --------------------------------------------------------------------------------------


def _runs_for_job(session: Session, job_id: int) -> list[PipelineRun]:
    """Return the backfill runs raised for one job, oldest first.

    Filtered in Python rather than by a JSON predicate: `metrics_json` is a `JSON` column, and a
    path query that only MySQL understands would make the smoke unrunnable against the SQLite
    fixture that mirrors it. The row count here is a handful.
    """
    return [
        run
        for run in session.scalars(
            select(PipelineRun).where(PipelineRun.run_type == "backfill").order_by(PipelineRun.id)
        ).all()
        if (run.metrics_json or {}).get("backfill_job_id") == job_id
    ]


def _repo_count(session: Session, prefix: str) -> int:
    """Return how many repositories the walk with one label has landed."""
    return int(
        session.scalar(
            select(func.count())
            .select_from(Repository)
            .where(
                Repository.provider_repo_id.startswith(f"{REPO_PREFIX}{prefix}_", autoescape=True)
            )
        )
        or 0
    )


def _backfill_checks(session: Session) -> dict[str, Any]:
    """Walk a history four ways: to the source's end, into a failure, out of it, and again."""
    jobs_before = int(session.scalar(select(func.count()).select_from(BackfillJob)) or 0)

    # -- the unsupported collector is refused before anything is written -----------------
    unsupported = ""
    try:
        start_backfill(session, collector=NotPaginated(), target_pages=1, per_page=1)
    except BackfillUnsupportedCollector as exc:
        unsupported = str(exc)
    else:
        raise RuntimeError("a collector that cannot page was accepted for a walk")

    # -- bounds are refused rather than stored and discovered later ----------------------
    bounds: list[str] = []
    for target_pages, per_page in ((0, 10), (101, 10), (2, 0), (2, 101)):
        try:
            create_job(
                session,
                provider="github",
                query={"q": SMOKE_QUERY},
                target_pages=target_pages,
                per_page=per_page,
            )
        except BackfillLimitError as exc:
            bounds.append(str(exc))
        else:
            raise RuntimeError(f"target_pages={target_pages} per_page={per_page} was accepted")

    # -- a walk stops at the source's end, not at the count the source advertises --------
    wide = MockGitHub(label="a")
    clock = FakeClock()
    limiter = RateLimiter(per_minute=PER_MINUTE, clock=clock.time, sleep=clock.sleep)
    first = start_backfill(
        session,
        collector=wide.collector,
        target_pages=MOCK_PAGE_COUNT + 2,
        per_page=MOCK_PER_PAGE,
        limiter=limiter,
        now=WALK_NOW,
    )
    first_repos = _repo_count(session, "a")

    # -- an interruption costs the page in flight, not the history ----------------------
    flaky = MockGitHub(label="b", failing_pages=(FAILING_PAGE,))
    fail_clock = FakeClock()
    fail_limiter = RateLimiter(per_minute=PER_MINUTE, clock=fail_clock.time, sleep=fail_clock.sleep)
    interrupted = ""
    interrupted_error = ""
    try:
        start_backfill(
            session,
            collector=flaky.collector,
            target_pages=MOCK_PAGE_COUNT,
            per_page=MOCK_PER_PAGE,
            limiter=fail_limiter,
            now=WALK_NOW,
        )
    except BackfillWalkFailed as exc:
        interrupted = str(exc)
        interrupted_error = exc.error_code
    else:
        raise RuntimeError("a walk whose page three failed reported success")
    failing_job = list_jobs(session, provider="github", limit=1)[0]
    failing_runs_before = _runs_for_job(session, failing_job.id)
    failing_run = failing_runs_before[-1]
    # Snapshotted here and not later: the job row is the cursor, so the resume below moves it.
    # Reading it afterwards would compare the resume's progress against the resume's
    # expectations and pass no matter where the interrupted walk actually stopped.
    failed_state = summarize_job(failing_job)
    repos_after_failure = _repo_count(session, "b")
    attempts_on_failing_page = flaky.requests_for(FAILING_PAGE)

    # -- the resume walks the rest, and only the rest -----------------------------------
    flaky.open_page(FAILING_PAGE)
    mark = len(flaky.requests)
    resumed = resume_backfill(
        session,
        job_id=failing_job.id,
        collector=flaky.collector,
        # The same limiter object as the failed attempt, on purpose: the outcome must report
        # what *this* walk spent. Reading the limiter's running total would credit the resume
        # with the waiting the failed attempt already did.
        limiter=fail_limiter,
        now=WALK_NOW + timedelta(minutes=1),
    )
    resumed_pages_requested = flaky.pages_requested_since(mark)
    repos_after_resume = _repo_count(session, "b")
    failing_runs_after = _runs_for_job(session, failing_job.id)
    resumed_state = summarize_job(failing_job)

    # -- walking the same pages again changes no row but the job's own counters ---------
    replay = MockGitHub(label="a")
    replay_outcome = start_backfill(
        session,
        collector=replay.collector,
        target_pages=MOCK_PAGE_COUNT,
        per_page=MOCK_PER_PAGE,
        limiter=RateLimiter(per_minute=PER_MINUTE, clock=FakeClock().time, sleep=lambda _: None),
        now=WALK_NOW,
    )
    repos_after_replay = _repo_count(session, "a")

    # -- the budget on the data source row is the budget the walk actually spends -------
    source = ensure_data_source(session, wide.collector)
    budget_clock = FakeClock()
    budget_job = create_job(
        session,
        provider="github",
        query=wide.collector.describe_query(),
        target_pages=2,
        per_page=2,
        now=WALK_NOW,
    )
    budget_seen: list[int] = []
    original_limiter = backfill_service.RateLimiter

    def spy_limiter(*, per_minute: int) -> RateLimiter:
        """Record the budget the walk derived, then pace on the fake clock."""
        budget_seen.append(per_minute)
        return original_limiter(
            per_minute=per_minute, clock=budget_clock.time, sleep=budget_clock.sleep
        )

    budget_collector = MockGitHub(label="c", page_count=6, total_count=6000)
    backfill_service.RateLimiter = spy_limiter  # type: ignore[assignment]
    try:
        budget_outcome = run_backfill(session, job=budget_job, collector=budget_collector.collector)
    finally:
        backfill_service.RateLimiter = original_limiter

    # -- a live cursor is not claimable twice -------------------------------------------
    guarded = create_job(
        session,
        provider="github",
        query={"q": SMOKE_QUERY},
        target_pages=3,
        per_page=2,
        now=WALK_NOW,
    )
    claim_job(session, guarded, now=WALK_NOW)
    busy = ""
    try:
        claim_job(session, guarded, now=WALK_NOW + timedelta(minutes=1))
    except BackfillJobBusy as exc:
        busy = exc.held_since
    else:
        raise RuntimeError("a job with a live heartbeat was claimed by a second walker")
    stale = claim_job(session, guarded, now=WALK_NOW + STALE_AFTER + timedelta(minutes=1))

    # -- a finished job refuses to be resumed -------------------------------------------
    not_resumable = ""
    try:
        resume_backfill(
            session,
            job_id=first.job_id,
            collector=wide.collector,
            limiter=limiter,
            now=WALK_NOW,
        )
    except BackfillNotResumable as exc:
        not_resumable = str(exc)
    else:
        raise RuntimeError("a completed walk was resumed")

    # Scoped to the jobs this run created. A smoke ledger that also counted rows an aborted
    # earlier run left behind would fail the second time it ran and pass the first, which is
    # the least useful way for a check to behave.
    smoke_job_ids = {
        first.job_id,
        failing_job.id,
        replay_outcome.job_id,
        budget_job.id,
        guarded.id,
    }
    smoke_job_refs = {str(job_id) for job_id in smoke_job_ids}
    audit = [
        entry
        for entry in session.scalars(
            select(AuditLog).where(AuditLog.action == "backfill.run").order_by(AuditLog.id)
        ).all()
        if entry.resource_id in smoke_job_refs
    ]
    jobs_after = int(session.scalar(select(func.count()).select_from(BackfillJob)) or 0)

    # -- assertions ---------------------------------------------------------------------
    if first.status != SUCCEEDED or not first.exhausted:
        raise RuntimeError(f"the wide walk did not finish cleanly: {first.status}")
    if first.pages_this_attempt != MOCK_PAGE_COUNT:
        raise RuntimeError(
            f"the wide walk requested {first.pages_this_attempt} pages, "
            f"but the source only serves {MOCK_PAGE_COUNT}"
        )
    if first.items_this_attempt != MOCK_PAGE_COUNT * MOCK_PER_PAGE:
        raise RuntimeError(f"the wide walk ingested {first.items_this_attempt} repositories")
    if first.next_page != MOCK_PAGE_COUNT + 1:
        raise RuntimeError(f"the cursor stopped at {first.next_page}")
    if clock.sleeps != [INTERVAL_SECONDS] * (MOCK_PAGE_COUNT - 1):
        raise RuntimeError(f"the wide walk paced {clock.sleeps}")
    if abs(first.paced_seconds - INTERVAL_SECONDS * (MOCK_PAGE_COUNT - 1)) > 1e-6:
        raise RuntimeError(f"the wide walk reported {first.paced_seconds} seconds of pacing")
    if first_repos != MOCK_PAGE_COUNT * MOCK_PER_PAGE:
        raise RuntimeError(f"the wide walk landed {first_repos} repository rows")

    if interrupted_error != "COLLECTOR_FAILED":
        raise RuntimeError(f"the failed walk was classified as {interrupted_error!r}")
    if failed_state.status != FAILED or failed_state.next_page != FAILING_PAGE:
        raise RuntimeError(
            f"the failed job stopped at {failed_state.status}/{failed_state.next_page}"
        )
    if failed_state.pages_completed != FAILING_PAGE - 1:
        raise RuntimeError(f"the failed job counted {failed_state.pages_completed} pages")
    if failed_state.items_collected != (FAILING_PAGE - 1) * MOCK_PER_PAGE:
        raise RuntimeError(f"the failed job counted {failed_state.items_collected} repositories")
    if failed_state.pages_remaining != MOCK_PAGE_COUNT - (FAILING_PAGE - 1):
        raise RuntimeError(f"the failed job reports {failed_state.pages_remaining} pages remaining")
    if not failed_state.resumable or failed_state.attempt != 1:
        raise RuntimeError("the failed job does not look like a resumable first attempt")
    if attempts_on_failing_page != 3:
        raise RuntimeError(
            f"page {FAILING_PAGE} was requested {attempts_on_failing_page} times; "
            "the retry policy should have tried three"
        )
    if repos_after_failure != (FAILING_PAGE - 1) * MOCK_PER_PAGE:
        raise RuntimeError(f"the interrupted walk landed {repos_after_failure} repository rows")
    if len(failing_runs_before) != 1:
        raise RuntimeError(
            f"the interrupted attempt left {len(failing_runs_before)} run rows; expected one"
        )
    if failing_run.status != "failed" or failing_run.error_code != "COLLECTOR_FAILED":
        raise RuntimeError(
            f"the failed walk's run row reads {failing_run.status}/{failing_run.error_code}"
        )
    if (failing_run.metrics_json or {}).get("next_page") != FAILING_PAGE:
        raise RuntimeError("the failed walk's run row does not record where the cursor stopped")

    if resumed_pages_requested != [FAILING_PAGE, FAILING_PAGE + 1]:
        raise RuntimeError(f"the resume requested pages {resumed_pages_requested}")
    if resumed.attempt != 2 or resumed.started_page != FAILING_PAGE:
        raise RuntimeError(
            f"the resume started at page {resumed.started_page} as attempt {resumed.attempt}"
        )
    if resumed.pages_completed != MOCK_PAGE_COUNT:
        raise RuntimeError(f"the resume finished {resumed.pages_completed} pages")
    if resumed.items_collected != MOCK_PAGE_COUNT * MOCK_PER_PAGE:
        raise RuntimeError(f"the resume counted {resumed.items_collected} repositories")
    if not resumed.exhausted:
        raise RuntimeError("the resume did not reach the end of the source")
    if fail_clock.sleeps != [
        INTERVAL_SECONDS,
        INTERVAL_SECONDS,
        INTERVAL_SECONDS,
        INTERVAL_SECONDS,
    ]:
        raise RuntimeError(f"the two attempts paced {fail_clock.sleeps}")
    if abs(resumed.paced_seconds - 2 * INTERVAL_SECONDS) > 1e-6:
        raise RuntimeError(
            f"the resume reported {resumed.paced_seconds} seconds of pacing; "
            "it must count only its own two intervals"
        )
    if repos_after_resume != MOCK_PAGE_COUNT * MOCK_PER_PAGE:
        raise RuntimeError(f"the resumed walk landed {repos_after_resume} repository rows")
    if resumed_state.status != SUCCEEDED or resumed_state.resumable:
        raise RuntimeError(
            f"after the resume the job reads {resumed_state.status} "
            f"with resumable={resumed_state.resumable}"
        )
    if resumed_state.attempt != 2 or resumed_state.pages_completed != MOCK_PAGE_COUNT:
        raise RuntimeError(
            f"the job's own counters read attempt={resumed_state.attempt} "
            f"pages={resumed_state.pages_completed} after the resume"
        )
    if len(failing_runs_after) != 2:
        raise RuntimeError(
            f"the interrupted job raised {len(failing_runs_after)} run rows in total; "
            "each attempt is supposed to leave one"
        )
    if [run.status for run in failing_runs_after] != ["failed", "succeeded"]:
        raise RuntimeError(f"the job's run rows read {[run.status for run in failing_runs_after]}")

    if replay_outcome.items_this_attempt != MOCK_PAGE_COUNT * MOCK_PER_PAGE:
        raise RuntimeError("the replay did not walk the same pages")
    if repos_after_replay != first_repos:
        raise RuntimeError(
            f"replaying the walk changed the repository count from {first_repos} "
            f"to {repos_after_replay}; ingestion is not idempotent"
        )

    if budget_seen != [source.rate_limit_per_minute]:
        raise RuntimeError(
            f"the walk built limiters for {budget_seen}; expected the source's "
            f"{source.rate_limit_per_minute}"
        )
    if budget_outcome.pages_this_attempt != 2 or budget_outcome.next_page != 3:
        raise RuntimeError("the budgeted walk did not stop on its page budget")
    if budget_outcome.exhausted:
        raise RuntimeError("a walk that stopped on its budget claimed to have exhausted the source")

    if not busy:
        raise RuntimeError("the busy guard did not report when the cursor was held")
    if stale.attempt != 2:
        raise RuntimeError("a stale heartbeat did not hand the cursor to the next walker")
    if "already completed" not in not_resumable:
        raise RuntimeError(f"resuming a finished walk said {not_resumable!r}")
    if [entry.result for entry in audit] != [
        "success",
        "failed",
        "success",
        "success",
        "success",
    ]:
        raise RuntimeError(f"the audit trail reads {[entry.result for entry in audit]}")
    if jobs_after - jobs_before != 5:
        raise RuntimeError(
            f"the run created {jobs_after - jobs_before} jobs; "
            "expected five and four refused attempts that wrote nothing"
        )

    return {
        "unsupported_collector": unsupported,
        "bound_refusals": bounds,
        "wide_pages_requested": wide.pages_requested(),
        "wide_exhausted": first.exhausted,
        "wide_next_page": first.next_page,
        "wide_items": first.items_this_attempt,
        "wide_sleeps": clock.sleeps,
        "wide_paced_seconds": first.paced_seconds,
        "wide_repository_rows": first_repos,
        "failed_error_code": interrupted_error,
        "failed_page": FAILING_PAGE,
        "failed_next_page": failed_state.next_page,
        "failed_pages_completed": failed_state.pages_completed,
        "failed_items_collected": failed_state.items_collected,
        "failed_pages_remaining": failed_state.pages_remaining,
        "failed_resumable": failed_state.resumable,
        "attempts_on_failing_page": attempts_on_failing_page,
        "repository_rows_after_failure": repos_after_failure,
        "failed_run_status": failing_run.status,
        "failed_run_error_code": failing_run.error_code,
        "runs_before_resume": len(failing_runs_before),
        "resumed_pages_requested": resumed_pages_requested,
        "resumed_attempt": resumed.attempt,
        "resumed_started_page": resumed.started_page,
        "resumed_pages_completed": resumed.pages_completed,
        "resumed_items_collected": resumed.items_collected,
        "resumed_exhausted": resumed.exhausted,
        "resumed_paced_seconds": resumed.paced_seconds,
        "resumed_job_status": resumed_state.status,
        "resumed_job_attempt": resumed_state.attempt,
        "resumed_job_pages_completed": resumed_state.pages_completed,
        "resumed_job_resumable": resumed_state.resumable,
        "runs_after_resume": len(failing_runs_after),
        "run_statuses_after_resume": [run.status for run in failing_runs_after],
        "sleeps_across_attempts": fail_clock.sleeps,
        "repository_rows_after_resume": repos_after_resume,
        "repository_rows_after_replay": repos_after_replay,
        "source_rate_limit_per_minute": source.rate_limit_per_minute,
        "derived_limiter_budgets": budget_seen,
        "budget_walk_pages": budget_outcome.pages_this_attempt,
        "budget_walk_exhausted": budget_outcome.exhausted,
        "busy_held_since": busy,
        "stale_takeover_attempt": stale.attempt,
        "not_resumable_message": not_resumable,
        "audit_results": [entry.result for entry in audit],
        "jobs_created": jobs_after - jobs_before,
        "first_job_id": first.job_id,
        "failed_job_id": failing_job.id,
        "interrupted_message": interrupted,
    }


# --------------------------------------------------------------------------------------
# Part two: the weekly report
# --------------------------------------------------------------------------------------


def _cache_readmes(session: Session) -> int:
    """Give every repository this smoke landed a cached README.

    A report section about a repository cannot be verified without evidence, and the only
    evidence the Agent's content tool will accept is content the collector already cached. A
    real deployment fills this in by fetching readmes; here the rows are written directly, and
    that is the one place in this script where the smoke writes data a pipeline stage would
    normally write.
    """
    repositories = session.scalars(
        select(Repository).where(
            Repository.provider_repo_id.startswith(REPO_PREFIX, autoescape=True)
        )
    ).all()
    written = 0
    for repository in repositories:
        existing = session.scalar(
            select(RepositoryContent).where(
                RepositoryContent.repository_id == repository.id,
                RepositoryContent.content_type == "readme",
            )
        )
        if existing is not None:
            continue
        session.add(
            RepositoryContent(
                repository_id=repository.id,
                content_type="readme",
                source_url=f"https://example.com/{repository.full_name}/readme",
                content=(
                    f"# {repository.full_name}\n\n"
                    f"A {repository.language} project tracked by the T16 smoke check.\n"
                    f"It matches {KEYWORD} and has {repository.stars} stars.\n"
                ),
                content_hash=f"{REPO_PREFIX}{repository.id}",
                fetched_at=BUSINESS_NOW,
            )
        )
        written += 1
    session.commit()
    return written


def _weekly_report_checks(session: Session, ids: dict[str, int]) -> dict[str, Any]:
    """Run collect -> snapshot -> rank -> research -> compose -> verify -> review -> publish."""
    settings = get_settings()
    topic_id = ids["topic_id"]

    daily = run_daily_pipeline(
        session,
        business_date=BUSINESS_DATE,
        collectors=[MockGitHub(label="daily").collector],
        limit=DAILY_LIMIT,
        attempt=1,
    )
    stages = [(stage.run_type, stage.status) for stage in daily.stages]
    candidates = list(
        session.scalars(
            select(TopicRepository)
            .where(TopicRepository.topic_id == topic_id)
            .order_by(TopicRepository.rank)
        ).all()
    )
    selected = [link for link in candidates if link.selected_for_agent]
    readmes = _cache_readmes(session)

    agent = run_research_agent(
        session,
        topic_id=topic_id,
        settings=settings,
        max_candidates=AGENT_CANDIDATES,
        now=BUSINESS_NOW,
    )
    generated = generate_report(
        session,
        agent_run_id=agent.agent_run_id,
        now=BUSINESS_NOW,
    )
    verification = verify_report(
        session,
        report_id=generated.report_id,
        settings=settings,
        now=BUSINESS_NOW,
    )
    decision = publish_or_request_review(
        session,
        report_id=generated.report_id,
        settings=settings,
        now=BUSINESS_NOW,
    )
    review = record_review(
        session,
        report_id=generated.report_id,
        decision="approve",
        reviewer_id=ids["user_id"],
        comment="T16 smoke approval",
        now=BUSINESS_NOW,
    )
    released = publish_due_reports(session, now=BUSINESS_NOW)
    report = session.get(Report, generated.report_id)
    if report is None:
        raise RuntimeError("the promoted report row disappeared")

    # -- assertions ---------------------------------------------------------------------
    if daily.status != "succeeded":
        raise RuntimeError(f"the daily pipeline closed as {daily.status}")
    for run_type in ("collect", "snapshot", "rank"):
        if (run_type, "succeeded") not in stages:
            raise RuntimeError(f"the {run_type} stage did not succeed: {stages}")
    if daily.provider_errors:
        raise RuntimeError(f"the collect stage reported {daily.provider_errors}")
    if daily.provider_counts != {"github": DAILY_LIMIT}:
        raise RuntimeError(f"the collect stage counted {daily.provider_counts}")
    if daily.snapshot_count < DAILY_LIMIT + 1:
        raise RuntimeError(f"the snapshot stage only covered {daily.snapshot_count} repositories")
    if daily.topic_candidate_counts.get(str(topic_id), 0) < DAILY_LIMIT:
        raise RuntimeError(f"ranking found {daily.topic_candidate_counts} candidates")
    if not selected:
        raise RuntimeError("no candidate was selected for the Agent")
    if len(selected) != AGENT_CANDIDATES:
        raise RuntimeError(
            f"the topic selected {len(selected)} candidates; the Agent reads exactly "
            f"{AGENT_CANDIDATES} before its tool budget runs out"
        )
    if len(candidates) <= AGENT_CANDIDATES:
        raise RuntimeError(
            f"only {len(candidates)} repositories matched the topic, so the cap was never hit"
        )
    if [link.rank for link in candidates] != list(range(1, len(candidates) + 1)):
        raise RuntimeError("candidate ranks are not contiguous")
    if readmes < 1:
        raise RuntimeError("no readme was cached for the Agent to read")

    if agent.status != "completed" or agent.error_code is not None:
        raise RuntimeError(f"the research run closed as {agent.status}/{agent.error_code}")
    if agent.budget_degraded or agent.guard_triggered:
        raise RuntimeError(
            f"the research run was degraded: budget={agent.budget_degraded} "
            f"guard={agent.guard_triggered}"
        )
    if agent.section_count != 6 or agent.report_draft_id is None:
        raise RuntimeError(f"the draft has {agent.section_count} sections")
    if agent.verification is None or not agent.verification.sufficient:
        raise RuntimeError("the Agent's own evidence check did not pass")
    if not agent.verification.content_repo_ids:
        raise RuntimeError("the Agent read no cached repository content")

    if not generated.created or generated.version != 1:
        raise RuntimeError(
            f"the first promotion reported created={generated.created} version={generated.version}"
        )
    if generated.source_count < 6:
        raise RuntimeError(f"the report carries {generated.source_count} citation rows")
    if not verification.passed:
        raise RuntimeError(f"the verifier rejected the report: {verification.reason_codes}")

    if decision.status != "pending_review" or not decision.requires_review:
        raise RuntimeError(f"the policy resolved to {decision.status}")
    if "FIRST_RUN" not in decision.reason_codes:
        raise RuntimeError(f"the policy reasons read {decision.reason_codes}")
    if review.status != "approved":
        raise RuntimeError(f"the approval left the report as {review.status}")

    mine = [entry for entry in released if entry["report_id"] == generated.report_id]
    if len(mine) != 1 or mine[0]["status"] != "published":
        raise RuntimeError(f"the release sweep reported {mine}")
    if report.status != "published" or report.published_at is None:
        raise RuntimeError(f"the report reads {report.status} after the release sweep")
    if report.published_at != BUSINESS_NOW:
        raise RuntimeError(f"the report was published at {report.published_at}")

    return {
        "daily_status": daily.status,
        "daily_stages": stages,
        "daily_provider_counts": daily.provider_counts,
        "daily_snapshot_count": daily.snapshot_count,
        "daily_candidate_counts": daily.topic_candidate_counts,
        "candidate_rows": len(candidates),
        "selected_rows": len(selected),
        "readmes_cached": readmes,
        "agent_status": agent.status,
        "agent_section_count": agent.section_count,
        "agent_tool_calls": agent.tool_call_count,
        "agent_model_calls": agent.model_call_count,
        "agent_evidence": agent.evidence_count,
        "agent_budget_degraded": agent.budget_degraded,
        "agent_guard_triggered": agent.guard_triggered,
        "agent_content_repos": len(agent.verification.content_repo_ids)
        if agent.verification
        else 0,
        "agent_prior_baseline": agent.verification.has_prior_baseline
        if agent.verification
        else False,
        "report_id": generated.report_id,
        "report_version": generated.version,
        "report_source_count": generated.source_count,
        "verification_passed": verification.passed,
        "verification_reasons": list(verification.reason_codes),
        "decision_status": decision.status,
        "decision_reasons": list(decision.reason_codes),
        "review_status": review.status,
        "released_count": len(released),
        "report_status": report.status,
        "report_published_at": report.published_at.isoformat() if report.published_at else None,
        "report_title": report.title,
    }


# --------------------------------------------------------------------------------------
# The real CLI, as a subprocess
# --------------------------------------------------------------------------------------


def _run_cli(*args: str) -> dict[str, Any]:
    """Run one real backfill CLI invocation and parse its JSON payload."""
    completed = subprocess.run(
        [sys.executable, "-m", "open_source_intelligence.backfill.cli", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"backfill CLI {' '.join(args)} failed: "
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _run_cli_expecting_failure(*args: str) -> tuple[int, str]:
    """Run a CLI invocation that must be refused, returning its exit code and message."""
    completed = subprocess.run(
        [sys.executable, "-m", "open_source_intelligence.backfill.cli", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        raise RuntimeError(f"backfill CLI {' '.join(args)} unexpectedly succeeded")
    return completed.returncode, (completed.stderr or completed.stdout).strip()


def _cli_checks(job_id: int) -> dict[str, Any]:
    """Drive the real CLI as a subprocess: the read actions and the two refusals."""
    listed = _run_cli("--action", "list", "--provider", "github", "--limit", "5")
    status = _run_cli("--action", "status", "--job-id", str(job_id))
    over_budget = _run_cli_expecting_failure(
        "--action", "start", "--provider", "github", "--target-pages", "500"
    )
    already_done = _run_cli_expecting_failure("--action", "resume", "--job-id", str(job_id))

    jobs = listed.get("jobs", [])
    if not jobs:
        raise RuntimeError("the CLI listed no backfill jobs")
    if any(job["provider"] != "github" for job in jobs):
        raise RuntimeError("the CLI listed a job from another provider")
    if status.get("job", {}).get("job_id") != job_id:
        raise RuntimeError(f"the CLI reported {status} for job {job_id}")
    if status["job"]["resumable"]:
        raise RuntimeError("the CLI calls a finished job resumable")
    if "between 1 and" not in over_budget[1]:
        raise RuntimeError(f"the page budget refusal reads {over_budget[1]!r}")
    if "already completed" not in already_done[1]:
        raise RuntimeError(f"the resume refusal reads {already_done[1]!r}")

    return {
        "listed_jobs": len(jobs),
        "listed_first_job": jobs[0]["job_id"],
        "status_job": status["job"]["job_id"],
        "status_status": status["job"]["status"],
        "status_resumable": status["job"]["resumable"],
        "over_budget_exit_code": over_budget[0],
        "over_budget_message": over_budget[1],
        "already_done_exit_code": already_done[0],
        "already_done_message": already_done[1],
    }


# --------------------------------------------------------------------------------------
# Fresh-session readback
# --------------------------------------------------------------------------------------


def readback(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Re-read the durable rows through a session that wrote nothing.

    Every assertion above ran in the session that performed the work, where a stale identity
    map can make an uncommitted change look committed. This pass asks the database directly.
    """
    backfill = payload["backfill"]
    report = payload["report"]

    finished_job = session.get(BackfillJob, backfill["first_job_id"])
    if finished_job is None:
        raise RuntimeError("the finished backfill job is not readable")
    failed_job = session.get(BackfillJob, backfill["failed_job_id"])
    if failed_job is None:
        raise RuntimeError("the interrupted backfill job is not readable")
    stored_report = session.get(Report, report["report_id"])
    if stored_report is None:
        raise RuntimeError("the published report is not readable")

    runs = list(
        session.scalars(
            select(PipelineRun).where(PipelineRun.run_type == "backfill").order_by(PipelineRun.id)
        ).all()
    )
    if runs and runs[-1].status != "succeeded":
        raise RuntimeError(f"the last backfill run reads {runs[-1].status}")
    resumed_runs = [
        run
        for run in runs
        if (run.metrics_json or {}).get("backfill_job_id") == backfill["failed_job_id"]
    ]
    verification = session.scalar(
        select(ReportVerification).where(
            ReportVerification.report_id == stored_report.id,
            ReportVerification.version == stored_report.current_version,
        )
    )
    approval = session.scalar(
        select(ReportReview).where(
            ReportReview.report_id == stored_report.id,
            ReportReview.decision == "approve",
        )
    )
    version = session.scalar(
        select(ReportVersion).where(
            ReportVersion.report_id == stored_report.id,
            ReportVersion.version == stored_report.current_version,
        )
    )
    sources = int(
        session.scalar(
            select(func.count())
            .select_from(ReportSource)
            .where(
                ReportSource.report_id == stored_report.id,
                ReportSource.version == stored_report.current_version,
            )
        )
        or 0
    )

    if finished_job.status != SUCCEEDED:
        raise RuntimeError(f"the finished job reads {finished_job.status} on re-read")
    if failed_job.next_page != MOCK_PAGE_COUNT + 1 or failed_job.attempt != 2:
        raise RuntimeError(
            f"the resumed job reads next_page={failed_job.next_page} attempt={failed_job.attempt} "
            "on re-read; the two attempts should have converged on the end of the source"
        )
    if [run.status for run in resumed_runs] != ["failed", "succeeded"]:
        raise RuntimeError(
            f"the resumed job's run rows read {[run.status for run in resumed_runs]} on re-read"
        )
    if stored_report.status != "published":
        raise RuntimeError(f"the report reads {stored_report.status} on re-read")
    if verification is None or verification.verdict != "passed":
        raise RuntimeError("the passing verdict did not survive the commit")
    if approval is None:
        raise RuntimeError("the approval did not survive the commit")
    if version is None:
        raise RuntimeError("the report version row did not survive the commit")
    if sources < 6:
        raise RuntimeError(f"only {sources} citation rows survived the commit")

    return {
        "finished_job_status": finished_job.status,
        "finished_job_next_page": finished_job.next_page,
        "finished_job_items": finished_job.items_collected,
        "resumed_job_status": failed_job.status,
        "resumed_job_attempt": failed_job.attempt,
        "resumed_job_next_page": failed_job.next_page,
        "resumed_job_items": failed_job.items_collected,
        "resumed_run_statuses": [run.status for run in resumed_runs],
        "backfill_runs": len(runs),
        "report_status": stored_report.status,
        "report_version": stored_report.current_version,
        "verification_verdict": verification.verdict,
        "approval_decision": approval.decision,
        "version_row": version.id is not None,
        "citation_rows": sources,
    }


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    """Parse the optional cleanup mode used by the verification script."""
    parser = argparse.ArgumentParser(description="Run T16 backfill and weekly report smoke checks")
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Only remove T16 smoke records and exit",
    )
    return parser.parse_args()


def main() -> None:
    """Run the T16 smoke checks on real MySQL and print the assertion payload."""
    args = _parse_args()
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    if args.cleanup_only:
        with session_factory() as session:
            removed = cleanup_verification_records(session)
        engine.dispose()
        print(json.dumps({"cleanup_only": True, "removed": removed}, ensure_ascii=False))
        return

    payload: dict[str, Any] = {}
    try:
        with session_factory() as session:
            cleanup_verification_records(session)
            ids = seed_topic(session)
            payload = {
                "backfill": _backfill_checks(session),
                "report": _weekly_report_checks(session, ids),
            }
            payload["cli"] = _cli_checks(payload["backfill"]["first_job_id"])
            payload["topic"] = {"topic_id": ids["topic_id"], "user_id": ids["user_id"]}
        with session_factory() as session:
            payload["readback"] = readback(session, payload)
    finally:
        for mock in _MOCKS:
            mock.close()
        with session_factory() as session:
            payload["removed"] = cleanup_verification_records(session)
        engine.dispose()

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
