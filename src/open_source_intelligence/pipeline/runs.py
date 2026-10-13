"""PipelineRun lifecycle, error classification, and run history queries.

Every deterministic or Agent stage writes one run record so operators can answer
"what ran, when, with which result, and why it failed" without reading logs.
Run records are committed as soon as they change state, because a failed process
must not be able to lose the evidence that it failed.
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

GLOBAL_RUN_TYPES: Final[tuple[str, ...]] = ("collect", "snapshot", "rank")
TOPIC_RUN_TYPES: Final[tuple[str, ...]] = ("agent", "publish")
RUN_TYPES: Final[tuple[str, ...]] = GLOBAL_RUN_TYPES + TOPIC_RUN_TYPES
RUN_STATUSES: Final[tuple[str, ...]] = ("queued", "running", "succeeded", "failed", "partial")
TERMINAL_STATUSES: Final[tuple[str, ...]] = ("succeeded", "failed", "partial")

ERROR_COLLECTOR_FAILED: Final[str] = "COLLECTOR_FAILED"
ERROR_SNAPSHOT_FAILED: Final[str] = "SNAPSHOT_FAILED"
ERROR_RANK_FAILED: Final[str] = "RANK_FAILED"
ERROR_UNEXPECTED: Final[str] = "PIPELINE_UNEXPECTED_ERROR"

STAGE_ERROR_CODES: Final[dict[str, str]] = {
    "collect": ERROR_COLLECTOR_FAILED,
    "snapshot": ERROR_SNAPSHOT_FAILED,
    "rank": ERROR_RANK_FAILED,
}

MAX_ERROR_MESSAGE_LENGTH: Final[int] = 2000


@dataclass(frozen=True, slots=True)
class PipelineRunSummary:
    """Serializable view of one pipeline run record."""

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
        """Return a JSON-ready payload for CLI, API, and log output."""
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


def _utc_now() -> datetime:
    """Return the current aware UTC timestamp."""
    return datetime.now(UTC)


def validate_run_type(run_type: str) -> str:
    """Reject run types that the database constraint would reject."""
    if run_type not in RUN_TYPES:
        raise ValueError(f"unsupported run_type: {run_type}")
    return run_type


def validate_run_status(status: str) -> str:
    """Reject statuses that the database constraint would reject."""
    if status not in RUN_STATUSES:
        raise ValueError(f"unsupported status: {status}")
    return status


def require_topic_scope(run_type: str, topic_id: int | None) -> None:
    """Keep Agent and publish runs bound to a topic, matching the database CHECK."""
    if run_type in TOPIC_RUN_TYPES and topic_id is None:
        raise ValueError(f"run_type {run_type} requires a topic_id")


def classify_error(run_type: str, error: BaseException) -> str:
    """Map an exception to a stable stage error code."""
    if isinstance(error, CollectorError):
        return ERROR_COLLECTOR_FAILED
    return STAGE_ERROR_CODES.get(run_type, ERROR_UNEXPECTED)


def describe_error(error: BaseException) -> str:
    """Redact and bound an exception message before it is persisted."""
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
    """Create a queued run record and persist it immediately."""
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
    """Move a queued run into the running state."""
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
    """Close a run with a terminal status, metrics, and optional error detail.

    `finished_at` exists for callers that pin the clock: the run was started at a moment
    they supplied, and `pipeline_runs` requires `finished_at >= started_at`, so closing it
    from the wall clock would make the record self-inconsistent. Callers that do not pin a
    clock leave it unset and the run closes on the wall clock as usual.
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
    """Close a run as failed with a stable error code and redacted message."""
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
    """Return one run record by primary key."""
    return session.get(PipelineRun, run_id)


def list_pipeline_runs(
    session: Session,
    *,
    topic_id: int | None = None,
    run_type: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> Sequence[PipelineRun]:
    """Return recent runs, newest first, filtered by optional criteria."""
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


def summarize_pipeline_run(run: PipelineRun) -> PipelineRunSummary:
    """Convert a run record into its serializable summary."""
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
    """Convert run records into JSON-ready payloads."""
    return [summarize_pipeline_run(run).to_dict() for run in runs]
