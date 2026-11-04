"""守护 Celery 的配置与任务执行。"""

from open_source_intelligence.core.config import Settings
from open_source_intelligence.workers.celery_app import (
    HEALTHCHECK_TASK,
    create_celery_app,
)
from open_source_intelligence.workers.tasks import healthcheck


def build_settings() -> Settings:
    """构造一套确定的 Celery 测试配置。"""
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://app:test@localhost:3306/intelligence",
        redis_url="redis://localhost:6379/15",
        secret_key="x" * 32,
        app_timezone="Asia/Shanghai",
    )


def test_celery_app_uses_redis_and_json_serialization() -> None:
    """证明 Celery 用的是配置好的 Redis broker 与 JSON 消息体。"""
    app = create_celery_app(build_settings())

    assert app.conf.broker_url == "redis://localhost:6379/15"
    assert app.conf.result_backend == "redis://localhost:6379/15"
    assert app.conf.task_serializer == "json"
    assert app.conf.timezone == "Asia/Shanghai"


def test_healthcheck_task_is_registered_and_eagerly_executable() -> None:
    """证明健康检查任务的逻辑在没有活的 Redis broker 时也能跑。"""
    app = create_celery_app(build_settings())

    assert HEALTHCHECK_TASK in app.tasks
    result = healthcheck.run()

    assert result == {"status": "ok", "task": "healthcheck"}
