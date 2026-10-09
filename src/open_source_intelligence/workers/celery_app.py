"""Celery application factory and shared runtime configuration."""

from celery import Celery

from open_source_intelligence.core.config import Settings, get_settings

HEALTHCHECK_TASK = "open_source_intelligence.workers.tasks.healthcheck"


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
        beat_schedule={
            "system-healthcheck": {
                "task": HEALTHCHECK_TASK,
                "schedule": 60.0,
            }
        },
    )
    return app
