"""供 Celery worker 与 beat 命令使用的 CLI 入口。"""

from celery.signals import setup_logging

from open_source_intelligence.core.config import get_settings
from open_source_intelligence.core.logging import configure_logging

from .celery_app import create_celery_app

settings = get_settings()
configure_logging(settings)
app = create_celery_app(settings)


@setup_logging.connect(weak=False)  # type: ignore[misc]
def configure_celery_logging(**_: object) -> None:
    """在 Celery 完成日志配置后重新应用共享的结构化日志器。"""
    configure_logging(settings)
