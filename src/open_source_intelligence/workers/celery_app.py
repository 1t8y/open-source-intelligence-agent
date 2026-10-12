"""Celery application factory and shared runtime configuration."""

from typing import Any

from celery import Celery
from celery.schedules import crontab

from open_source_intelligence.core.config import Settings, get_settings

HEALTHCHECK_TASK = "open_source_intelligence.workers.tasks.healthcheck"
DAILY_PIPELINE_TASK = "open_source_intelligence.workers.tasks.run_daily_pipeline"
RESEARCH_AGENT_TASK = "open_source_intelligence.workers.tasks.run_research_agent"


def build_beat_schedule(settings: Settings) -> dict[str, dict[str, Any]]:
    """Return the periodic schedule, including the daily deterministic pipeline."""
    schedule: dict[str, dict[str, Any]] = {
        "system-healthcheck": {
            "task": HEALTHCHECK_TASK,
            "schedule": 60.0,
        }
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
