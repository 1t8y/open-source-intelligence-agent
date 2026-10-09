"""Celery tasks for worker and scheduler health verification."""

import httpx
from celery import shared_task

from open_source_intelligence.collectors.cli import build_collectors
from open_source_intelligence.collectors.service import collect_repositories
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)


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
