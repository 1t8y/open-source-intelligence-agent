"""Daily deterministic pipeline orchestration with one PipelineRun per stage.

The order is fixed and rule-driven: collect -> snapshot -> rank -> contents. No language
model is involved, so a rerun with the same inputs produces the same candidate ranking.
Each stage writes its own run record, which makes a partially failed batch
diagnosable without reading container logs.

`contents` is the last stage and the only optional one: it pre-warms the README cache for the
ranked candidates, and it needs a network source to do anything. Callers that pass no content
source get the three deterministic stages and no contents run record, which is exactly what a
run with nothing to fetch should look like.
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
    """Collection stage outcome with per-provider counters."""

    stage: StageOutcome
    provider_counts: dict[str, int]
    provider_errors: dict[str, str]


@dataclass(frozen=True, slots=True)
class DailyPipelineResult:
    """Aggregated result of one daily deterministic pipeline execution."""

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
        """Return a JSON-ready payload for CLI, worker, and log output."""
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
    """Return enabled topics in a stable order."""
    return session.scalars(select(Topic).where(Topic.enabled.is_(True)).order_by(Topic.id)).all()


def run_collect_stage(
    session: Session,
    *,
    business_date: date,
    collectors: Sequence[RepositoryCollector],
    limit: int,
    attempt: int = 1,
) -> CollectStageOutcome:
    """Collect from every requested provider and record the batch.

    A provider failure is recorded and tolerated while another provider still
    returns data. Only a batch where every provider failed is fatal, because an
    empty collection makes the downstream slices meaningless.
    """
    if not collectors:
        raise ValueError("at least one collector is required")
    run = start_pipeline_run(session, run_type="collect", topic_id=None, attempt=attempt)
    mark_pipeline_run_running(session, run)
    summary = collect_repositories(session, collectors, limit=limit)
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
    """Create or refresh the daily snapshots and record the run."""
    run = start_pipeline_run(session, run_type="snapshot", topic_id=None, attempt=attempt)
    mark_pipeline_run_running(session, run)
    try:
        snapshot_count = create_daily_snapshots(session, snapshot_date=business_date)
    except Exception as exc:  # noqa: BLE001 - recorded and re-raised as a stage error
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
    """Rank one topic and record the run without failing the whole batch."""
    run = start_pipeline_run(session, run_type="rank", topic_id=topic.id, attempt=attempt)
    mark_pipeline_run_running(session, run)
    try:
        ranked = rank_topic_candidates(session, topic, as_of_date=business_date)
    except Exception as exc:  # noqa: BLE001 - one topic must not stop the others
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
    """Rank every enabled topic, one run record per topic."""
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
    """Run collect, snapshot, rank, and (optionally) contents as one observable batch.

    `content_sources` is empty by default, and an empty list means the contents stage does not
    run at all: no requests, no run record, `content_counts` empty. That default exists because
    the content stage is the only one that talks to a per-repository endpoint, and a caller that
    cannot supply one (a fixture, a drill, an offline replay) should get the deterministic three
    stages rather than a stage that reports zero successes.

    `content_limit` defaults to `limit` - the same order of magnitude as the collection batch -
    so a caller that does not think about it still gets a bounded number of requests.

    Raises:
        PipelineStageError: when collection, snapshotting, or the contents stage itself fails.
            Ranking failures stay inside the affected topic and are recorded as a failed rank
            run; a single repository whose content cannot be fetched is counted, not raised.
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
