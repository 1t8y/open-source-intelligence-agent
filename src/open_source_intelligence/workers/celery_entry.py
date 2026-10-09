"""CLI entry point used by Celery worker and beat commands."""

from celery.signals import setup_logging

from open_source_intelligence.core.config import get_settings
from open_source_intelligence.core.logging import configure_logging

from .celery_app import create_celery_app

settings = get_settings()
configure_logging(settings)
app = create_celery_app(settings)


@setup_logging.connect(weak=False)  # type: ignore[misc]
def configure_celery_logging(**_: object) -> None:
    """Reapply the shared structured logger after Celery logging setup."""
    configure_logging(settings)
