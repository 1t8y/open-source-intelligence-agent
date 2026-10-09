"""Celery tasks for worker and scheduler health verification."""

from celery import shared_task


@shared_task(name="open_source_intelligence.workers.tasks.healthcheck")  # type: ignore[misc]
def healthcheck() -> dict[str, str]:
    """Return a stable payload proving worker execution."""
    return {"status": "ok", "task": "healthcheck"}
