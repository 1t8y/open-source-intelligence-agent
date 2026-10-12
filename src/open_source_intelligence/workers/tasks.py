"""Celery tasks for the deterministic pipeline and worker health verification."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from celery import Task, shared_task

from open_source_intelligence.collectors.cli import build_collectors
from open_source_intelligence.collectors.service import collect_repositories
from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.pipeline.daily import PipelineStageError
from open_source_intelligence.pipeline.daily import run_daily_pipeline as execute_daily_pipeline
from open_source_intelligence.workers.celery_app import DAILY_PIPELINE_TASK


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
