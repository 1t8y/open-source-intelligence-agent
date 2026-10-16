"""Repository content cache refresh: the only production writer of `repository_contents`.

`repository_contents` is what makes a research run self-contained. The `get_repository_content`
tool answers from that table and never reaches for the network, so the Agent can read a README
mid-run without an outbound request, and a report citation to that README resolves against a row
that exists. Something has to fill the table; until T23 nothing did, so the tool always failed
with `TOOL_NOT_FOUND`, verification always concluded "no cached repository content was read", and
report generation died on `readme source not found`.

Two placement decisions carry the design:

* **It runs after ranking, not inside collection.** The question this stage needs answered is
  "which repositories are worth pre-warming", and the ranking is that answer. Fetching for every
  known repository would spend one request each per day to cache documents the next run never
  reads, because only the selected candidates are researched - and against GitHub's
  unauthenticated budget that is the difference between a comfortable daily batch and one that
  runs out mid-walk.
* **A failure here is a count, not an exception.** One repository whose README cannot be fetched
  must not end the day's batch, for the same reason one failing provider does not: the work that
  could be done is still worth doing. The run record keeps "fetched", "skipped", "missing" and
  "failed" apart, so "this repository has no README" and "we never found out" stay distinguishable
  after the fact.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.collectors.service import upsert_repository_content
from open_source_intelligence.collectors.types import (
    RepositoryContentSource,
    SourceUnavailableError,
)
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    RepositoryContent,
    Topic,
    TopicRepository,
)
from open_source_intelligence.pipeline.runs import (
    ERROR_CONTENT_FAILED,
    PipelineStageError,
    StageOutcome,
    classify_error,
    describe_error,
    fail_pipeline_run,
    finish_pipeline_run,
    mark_pipeline_run_running,
    start_pipeline_run,
)
from open_source_intelligence.security.audit import record_audit_event

#: The content kind this stage fills. Matches the `repository_contents` CHECK, which allows
#: only `readme`; the constant exists so the candidate query, the skip check, and the write all
#: name the same thing.
CACHE_CONTENT_TYPE = "readme"

#: How much of a failed fetch's message reaches the run record. The message is already redacted
#: by `describe_error`; this keeps twenty failures from turning one metrics blob into a log file.
MAX_FAILURE_MESSAGE_CHARS = 200


@dataclass(frozen=True, slots=True)
class ContentCandidate:
    """One repository the ranking suggests pre-warming, with where it came from."""

    repository_id: int
    full_name: str
    provider: str
    topic_id: int
    rank: int


@dataclass(slots=True)
class ContentRefreshSummary:
    """Per-outcome counts for one refresh pass."""

    fetched: int = 0
    skipped: int = 0
    missing: int = 0
    failed: int = 0
    unsupported: int = 0
    failures: dict[int, str] = field(default_factory=dict)

    def handled_without_failure(self) -> int:
        """Return how many candidates reached a definite, non-failure outcome."""
        return self.fetched + self.skipped + self.missing + self.unsupported


@dataclass(frozen=True, slots=True)
class ContentsStageOutcome:
    """The stage's run record plus the counts an operator reads first."""

    stage: StageOutcome
    candidate_count: int
    summary: ContentRefreshSummary


def business_day_start(business_date: date, timezone_name: str) -> datetime:
    """Return the UTC instant at which the given business day began.

    `refresh_after` is compared against `repository_contents.fetched_at`, which is stored in UTC,
    so the boundary has to be the start of that day *in the app timezone* converted to UTC.
    Midnight UTC would land mid-morning in Asia/Shanghai, which would make the 02:00 scheduled
    run land "yesterday" and re-fetch everything on every subsequent run of the same day.
    """
    timezone = ZoneInfo(timezone_name)
    return datetime.combine(business_date, time.min, tzinfo=timezone).astimezone(UTC)


def select_content_candidates(session: Session, *, limit: int) -> list[ContentCandidate]:
    """Return the repositories worth pre-warming, best-ranked first, without duplicates.

    The selection is the selected candidates of every *enabled* topic, ordered by
    `(topic_id, rank)` - the same rows a research run is allowed to read, which is what makes the
    cache cover the authorization scope instead of a guess at it.

    A repository selected by two topics appears once, at the better of its two positions: the
    cache is keyed by repository, so fetching it twice would repeat the same request, and the
    order between topics is not a real preference.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    rows = session.execute(
        select(
            TopicRepository.repository_id,
            TopicRepository.topic_id,
            TopicRepository.rank,
            Repository.full_name,
            DataSource.source_type,
        )
        .join(Topic, Topic.id == TopicRepository.topic_id)
        .join(Repository, Repository.id == TopicRepository.repository_id)
        .join(DataSource, DataSource.id == Repository.source_id)
        .where(
            Topic.enabled.is_(True),
            TopicRepository.selected_for_agent.is_(True),
        )
        .order_by(TopicRepository.topic_id, TopicRepository.rank)
    ).all()
    candidates: list[ContentCandidate] = []
    seen: set[int] = set()
    for repository_id, topic_id, rank, full_name, provider in rows:
        if repository_id in seen:
            continue
        seen.add(repository_id)
        candidates.append(
            ContentCandidate(
                repository_id=repository_id,
                full_name=full_name,
                provider=provider,
                topic_id=topic_id,
                rank=rank,
            )
        )
        if len(candidates) == limit:
            break
    return candidates


def refresh_repository_contents(
    session: Session,
    *,
    candidates: Sequence[ContentCandidate],
    sources: Mapping[str, RepositoryContentSource],
    refresh_after: datetime | None = None,
) -> ContentRefreshSummary:
    """Fetch and cache content for each candidate, committing one repository at a time.

    Committing per repository is deliberate: a fetch that fails halfway through must not take the
    repositories already fetched down with it, and the cache is a set of independent rows with no
    invariant tying them together.

    `refresh_after` is the idempotence window. When it is set, a repository whose cached text was
    confirmed at or after that moment is skipped without a request; when it is `None`, every
    candidate is asked about, which is what a forced refresh wants.
    """
    summary = ContentRefreshSummary()
    for candidate in candidates:
        source = sources.get(candidate.provider)
        if source is None:
            summary.unsupported += 1
            continue
        if refresh_after is not None and _confirmed_since(session, candidate, refresh_after):
            summary.skipped += 1
            continue
        try:
            fetched = source.fetch_readme(candidate.full_name)
        except SourceUnavailableError as exc:
            summary.failed += 1
            summary.failures[candidate.repository_id] = describe_error(exc)[
                :MAX_FAILURE_MESSAGE_CHARS
            ]
            continue
        if fetched is None:
            summary.missing += 1
            continue
        repository = session.get(Repository, candidate.repository_id)
        if repository is None:  # pragma: no cover - the candidate query produced this id
            summary.failed += 1
            summary.failures[candidate.repository_id] = "repository row disappeared"
            continue
        upsert_repository_content(session, repository=repository, fetched=fetched)
        summary.fetched += 1
        session.commit()
    return summary


def _confirmed_since(session: Session, candidate: ContentCandidate, moment: datetime) -> bool:
    """Return whether this candidate's cache was refreshed at or after `moment`.

    Compared in SQL rather than in Python so the stored value is read with the same type
    conversion that wrote it: the column is UTC, and comparing a naive read-back against an aware
    argument is the kind of mismatch that silently makes every repository look stale.
    """
    found = session.scalar(
        select(RepositoryContent.id)
        .where(
            RepositoryContent.repository_id == candidate.repository_id,
            RepositoryContent.content_type == CACHE_CONTENT_TYPE,
            RepositoryContent.fetched_at >= moment,
        )
        .limit(1)
    )
    return found is not None


def _stage_status(summary: ContentRefreshSummary, *, candidate_count: int) -> str:
    """Map the outcome counts onto the run status vocabulary."""
    if summary.failed == 0:
        return "succeeded"
    # Nothing reached a definite answer: this run failed rather than partly succeeded. A
    # selection of zero cannot produce failures, so reaching here already means there was work.
    if summary.handled_without_failure() == 0 and candidate_count > 0:
        return "failed"
    return "partial"


def run_contents_stage(
    session: Session,
    *,
    business_date: date,
    sources: Sequence[RepositoryContentSource],
    limit: int,
    refresh_after: datetime | None = None,
    attempt: int = 1,
) -> ContentsStageOutcome:
    """Refresh the content cache for the top-ranked candidates and record the run.

    Raises:
        PipelineStageError: when the stage itself cannot run (for example the candidate query
            fails). One repository failing to fetch is counted instead, and the exception carries
            the run id of the record that already exists for the failure.
        ValueError: when no source is supplied. An empty source list means "nobody can answer
            this question", and recording a run that did nothing would put a green row in the
            ledger for a stage that never ran; the daily orchestrator skips the stage instead.
    """
    if not sources:
        raise ValueError("at least one content source is required")
    by_provider = {source.provider: source for source in sources}
    run = start_pipeline_run(session, run_type="contents", topic_id=None, attempt=attempt)
    mark_pipeline_run_running(session, run)
    try:
        candidates = select_content_candidates(session, limit=limit)
        summary = refresh_repository_contents(
            session,
            candidates=candidates,
            sources=by_provider,
            refresh_after=refresh_after,
        )
    except Exception as exc:  # noqa: BLE001 - recorded and re-raised as a stage error
        error_code = classify_error("contents", exc)
        message = describe_error(exc)
        fail_pipeline_run(session, run, error_code=error_code, error_message=message)
        raise PipelineStageError(
            message,
            run_id=run.id,
            run_type="contents",
            error_code=error_code,
        ) from exc
    status = _stage_status(summary, candidate_count=len(candidates))
    metrics: dict[str, Any] = {
        "business_date": business_date.isoformat(),
        "candidate_count": len(candidates),
        "limit": limit,
        "refresh_after": refresh_after.isoformat() if refresh_after is not None else None,
        "fetched": summary.fetched,
        "skipped": summary.skipped,
        "missing": summary.missing,
        "unsupported": summary.unsupported,
        "failed": summary.failed,
        "failures": {str(key): value for key, value in sorted(summary.failures.items())},
    }
    if status == "failed":
        fail_pipeline_run(
            session,
            run,
            error_code=ERROR_CONTENT_FAILED,
            error_message=f"no repository content could be fetched ({summary.failed} failed)",
            metrics_json=metrics,
        )
    else:
        finish_pipeline_run(session, run, status=status, metrics_json=metrics)
    record_audit_event(
        session,
        action="content.refresh",
        resource_type="pipeline",
        resource_id=str(run.id),
        # The audit vocabulary is coarser than the run vocabulary: `partial` is not one of its
        # values, and a pass where some repositories could not be fetched belongs on the strict
        # side of the line rather than the permissive one.
        result="success" if status == "succeeded" else "failed",
        metadata_json={
            "business_date": metrics["business_date"],
            "candidate_count": len(candidates),
            "fetched": summary.fetched,
            "skipped": summary.skipped,
            "missing": summary.missing,
            "unsupported": summary.unsupported,
            "failed": summary.failed,
        },
    )
    session.commit()
    return ContentsStageOutcome(
        stage=StageOutcome(
            run_id=run.id,
            run_type="contents",
            topic_id=None,
            status=status,
            metrics=metrics,
        ),
        candidate_count=len(candidates),
        summary=summary,
    )
