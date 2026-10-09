"""Tests for Celery configuration and task execution."""

from open_source_intelligence.core.config import Settings
from open_source_intelligence.workers.celery_app import (
    HEALTHCHECK_TASK,
    create_celery_app,
)
from open_source_intelligence.workers.tasks import healthcheck


def build_settings() -> Settings:
    """Create deterministic Celery test settings."""
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://app:test@localhost:3306/intelligence",
        redis_url="redis://localhost:6379/15",
        secret_key="x" * 32,
        app_timezone="Asia/Shanghai",
    )


def test_celery_app_uses_redis_and_json_serialization() -> None:
    """Celery must use the configured Redis broker and JSON payloads."""
    app = create_celery_app(build_settings())

    assert app.conf.broker_url == "redis://localhost:6379/15"
    assert app.conf.result_backend == "redis://localhost:6379/15"
    assert app.conf.task_serializer == "json"
    assert app.conf.timezone == "Asia/Shanghai"


def test_healthcheck_task_is_registered_and_eagerly_executable() -> None:
    """The health task logic should execute without a live Redis broker in tests."""
    app = create_celery_app(build_settings())

    assert HEALTHCHECK_TASK in app.tasks
    result = healthcheck.run()

    assert result == {"status": "ok", "task": "healthcheck"}
