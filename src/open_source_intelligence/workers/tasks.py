"""Celery tasks for the deterministic pipeline and worker health verification."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from celery import Task, shared_task

from open_source_intelligence.agent.errors import AGENT_MODEL_UNAVAILABLE
from open_source_intelligence.agent.runtime import run_research_agent as execute_research_agent
from open_source_intelligence.collectors.cli import build_collectors
from open_source_intelligence.collectors.service import collect_repositories
from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.notifications.service import PUSH_BATCH_LIMIT
from open_source_intelligence.notifications.service import (
    retry_failed_pushes as execute_retry_failed_pushes,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.pipeline.daily import PipelineStageError
from open_source_intelligence.pipeline.daily import run_daily_pipeline as execute_daily_pipeline
from open_source_intelligence.reports.publishing import (
    PUBLISH_BATCH_LIMIT,
)
from open_source_intelligence.reports.publishing import (
    publish_due_reports as execute_publish_due_reports,
)
from open_source_intelligence.workers.celery_app import (
    DAILY_PIPELINE_TASK,
    PUBLISH_DUE_TASK,
    PUSH_RETRY_TASK,
    RESEARCH_AGENT_TASK,
)


def resolve_business_date(value: str | None, settings: Settings) -> date:
    """Resolve an explicit business date or today in the configured timezone."""
    if value:
        return date.fromisoformat(value)
    return datetime.now(ZoneInfo(settings.app_timezone)).date()


def pipeline_retry_countdown(*, retries: int, settings: Settings) -> int:
    """Return exponential backoff seconds before the next pipeline attempt."""
    # 1 << retries is 2**retries with an int result that mypy can follow.
    return settings.pipeline_retry_backoff_seconds * (1 << retries)


@shared_task(name="open_source_intelligence.workers.tasks.healthcheck")  # type: ignore[misc]
def healthcheck() -> dict[str, str]:
    """Return a stable payload proving worker execution."""
    return {"status": "ok", "task": "healthcheck"}


@shared_task(name="open_source_intelligence.workers.tasks.collect_sources")  # type: ignore[misc]
def collect_sources(provider: str = "all", limit: int = 5) -> dict[str, object]:
    """Collect one repository batch and return provider counts and errors."""
    if provider not in {"github", "huggingface", "all"}:
        raise ValueError("provider must be github, huggingface, or all")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")

    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            with session_factory() as session:
                summary = collect_repositories(
                    session,
                    build_collectors(provider, client),
                    limit=limit,
                )
        return {
            "status": "ok",
            "provider_counts": summary.provider_counts,
            "errors": summary.errors,
        }
    finally:
        engine.dispose()


@shared_task(bind=True, name=DAILY_PIPELINE_TASK)  # type: ignore[misc]
def run_daily_pipeline(
    self: Task,
    *,
    provider: str = "all",
    limit: int | None = None,
    business_date: str | None = None,
) -> dict[str, Any]:
    """Run collect, snapshot, and rank once, retrying transient stage failures.

    Each attempt writes its own PipelineRun records, so the retry trail stays
    visible: attempt 1 fails, attempt 2 succeeds, and both remain queryable.
    """
    if provider not in {"github", "huggingface", "all"}:
        raise ValueError("provider must be github, huggingface, or all")
    settings = get_settings()
    resolved_limit = settings.pipeline_collect_limit if limit is None else limit
    if not 1 <= resolved_limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    attempt = self.request.retries + 1
    resolved_date = resolve_business_date(business_date, settings)
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            collectors = build_collectors(provider, client)
            with session_factory() as session:
                result = execute_daily_pipeline(
                    session,
                    business_date=resolved_date,
                    collectors=collectors,
                    limit=resolved_limit,
                    attempt=attempt,
                )
    except PipelineStageError as exc:
        if self.request.retries < settings.pipeline_task_max_retries:
            raise self.retry(
                exc=exc,
                countdown=pipeline_retry_countdown(
                    retries=self.request.retries,
                    settings=settings,
                ),
            ) from exc
        raise
    finally:
        engine.dispose()
    payload: dict[str, Any] = result.to_dict()
    payload["task"] = DAILY_PIPELINE_TASK
    return payload


def _optional_datetime(value: str | None) -> datetime | None:
    """Parse an optional ISO 8601 timestamp from a task payload."""
    return None if value is None else datetime.fromisoformat(value)


@shared_task(bind=True, name=RESEARCH_AGENT_TASK)  # type: ignore[misc]
def run_research_agent(
    self: Task,
    *,
    topic_id: int,
    window_start: str | None = None,
    window_end: str | None = None,
    max_candidates: int | None = None,
) -> dict[str, Any]:
    """Run one bounded research Agent for a topic.

    Transient model transport failures are retried with the shared pipeline backoff. Every
    other outcome is returned as-is, because a draft that needs review is a result, not an
    error: only model transport problems are worth retrying.
    """
    if topic_id <= 0:
        raise ValueError("topic_id must be positive")
    if max_candidates is not None and max_candidates <= 0:
        raise ValueError("max_candidates must be positive")
    settings = get_settings()
    if not settings.agent_enabled:
        raise ValueError("the research Agent is disabled by AGENT_ENABLED")
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            result = execute_research_agent(
                session,
                topic_id=topic_id,
                settings=settings,
                window_start=_optional_datetime(window_start),
                window_end=_optional_datetime(window_end),
                max_candidates=max_candidates,
            )
    finally:
        engine.dispose()
    if (
        result.status == "failed"
        and result.error_code == AGENT_MODEL_UNAVAILABLE
        and self.request.retries < settings.pipeline_task_max_retries
    ):
        raise self.retry(
            countdown=pipeline_retry_countdown(
                retries=self.request.retries,
                settings=settings,
            ),
        )
    payload: dict[str, Any] = result.to_dict()
    payload["task"] = RESEARCH_AGENT_TASK
    return payload


@shared_task(name=PUBLISH_DUE_TASK)  # type: ignore[misc]
def publish_due_reports(limit: int = PUBLISH_BATCH_LIMIT) -> dict[str, Any]:
    """Release the reports whose withdrawal window has elapsed.

    The sweep is idempotent: a report is only released while it is `scheduled` and past its
    `publish_at`, so a repeated run finds nothing left to do. A report that turns out to
    violate the publish policy is marked `failed` with its stable error code instead of
    aborting the batch.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            released = execute_publish_due_reports(session, limit=limit)
    finally:
        engine.dispose()
    published_count = sum(1 for item in released if item["status"] == "published")
    return {
        "status": "ok",
        "task": PUBLISH_DUE_TASK,
        "released_count": published_count,
        "blocked_count": len(released) - published_count,
        "released": released,
    }


@shared_task(name=PUSH_RETRY_TASK)  # type: ignore[misc]
def retry_failed_pushes(limit: int = PUSH_BATCH_LIMIT) -> dict[str, Any]:
    """Retry the report pushes whose transport refused them earlier.

    Retrying here rather than in the publishing sweep keeps the two concerns apart: a release
    that is due must not wait behind a mail server, and a delivery that failed has to be
    retried on its own schedule. The sweep is idempotent, because a push row only stays a
    candidate while it is `failed` and still inside its retry budget.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            outcomes = execute_retry_failed_pushes(session, limit=limit, settings=settings)
    finally:
        engine.dispose()
    delivered = [item for item in outcomes if item.delivered]
    return {
        "status": "ok",
        "task": PUSH_RETRY_TASK,
        "delivered_count": len(delivered),
        "failed_count": len(outcomes) - len(delivered),
        "attempted_count": len(outcomes),
        "outcomes": [item.to_dict() for item in outcomes],
    }
