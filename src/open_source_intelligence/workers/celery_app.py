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


def build_beat_schedule(settings: Settings) -> dict[str, dict[str, Any]]:
    """Return the periodic schedule for the pipeline and the publishing sweep.

    The publishing sweep is always on: releasing reports whose withdrawal window has
    elapsed consumes no model budget, and leaving it manual would defeat the point of
    `AUTO_WITH_DELAY`.
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
