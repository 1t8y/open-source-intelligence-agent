"""Celery application factory and shared runtime configuration."""

from datetime import timedelta
from typing import Any

from celery import Celery
from celery.schedules import crontab

from open_source_intelligence.core.config import Settings, get_settings

HEALTHCHECK_TASK = "open_source_intelligence.workers.tasks.healthcheck"
DAILY_PIPELINE_TASK = "open_source_intelligence.workers.tasks.run_daily_pipeline"
RESEARCH_AGENT_TASK = "open_source_intelligence.workers.tasks.run_research_agent"
PUBLISH_DUE_TASK = "open_source_intelligence.workers.tasks.publish_due_reports"
PUSH_RETRY_TASK = "open_source_intelligence.workers.tasks.retry_failed_pushes"
BACKFILL_TASK = "open_source_intelligence.workers.tasks.run_backfill"

BACKFILL_TIME_LIMIT_SECONDS = 1800
BACKFILL_SOFT_TIME_LIMIT_SECONDS = 1740
"""A paced walk outgrows the worker's five-minute default on purpose.

Its own bounds are what stop it: `MAX_TARGET_PAGES` pages, each held back by the source's
configured request budget. A hundred pages at the default thirty requests per minute is
already over three minutes before any request latency, and a source configured down to ten
per minute needs ten. Killing the walk at five minutes would abort a job that was behaving
exactly as configured - and because the job is resumable that would be survivable, but it
would burn the rate-limit budget of an attempt that could never finish."""


def build_beat_schedule(settings: Settings) -> dict[str, dict[str, Any]]:
    """Return the periodic schedule for the pipeline, the publishing sweep, and push retries.

    The publishing sweep is always on: releasing reports whose withdrawal window has
    elapsed consumes no model budget, and leaving it manual would defeat the point of
    `AUTO_WITH_DELAY`. Push retries are scheduled separately so a slow or unavailable mail
    server cannot delay a release that is already due.

    The backfill is deliberately absent. It is an operator's deliberate, one-off, expensive
    act against a shared public API, so it is dispatched on demand rather than by a clock
    that would start walking history nobody asked for.
    """
    schedule: dict[str, dict[str, Any]] = {
        "system-healthcheck": {
            "task": HEALTHCHECK_TASK,
            "schedule": 60.0,
        },
        "publish-due-reports": {
            "task": PUBLISH_DUE_TASK,
            "schedule": timedelta(minutes=settings.publish_sweep_minutes),
        },
        "retry-failed-pushes": {
            "task": PUSH_RETRY_TASK,
            "schedule": timedelta(minutes=settings.push_retry_interval_minutes),
        },
    }
    if settings.pipeline_schedule_enabled:
        schedule["daily-deterministic-pipeline"] = {
            "task": DAILY_PIPELINE_TASK,
            "schedule": crontab(
                hour=settings.pipeline_schedule_hour,
                minute=settings.pipeline_schedule_minute,
            ),
            "kwargs": {
                "provider": "all",
                "limit": settings.pipeline_collect_limit,
            },
        }
    return schedule


def create_celery_app(settings: Settings | None = None) -> Celery:
    """Create and configure a Celery application without connecting to Redis."""
    resolved_settings = settings or get_settings()
    app = Celery("open_source_intelligence")
    app.conf.update(
        broker_url=resolved_settings.redis_url,
        result_backend=resolved_settings.redis_url,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone=resolved_settings.app_timezone,
        enable_utc=True,
        task_track_started=True,
        task_time_limit=300,
        task_soft_time_limit=240,
        imports=("open_source_intelligence.workers.tasks",),
        beat_schedule=build_beat_schedule(resolved_settings),
    )
    return app
