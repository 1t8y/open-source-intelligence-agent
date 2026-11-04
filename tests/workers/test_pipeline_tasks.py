"""守护每日流水线的调度、重试策略与业务日期。"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from celery.schedules import crontab

from open_source_intelligence.core.config import Settings
from open_source_intelligence.workers.celery_app import (
    DAILY_PIPELINE_TASK,
    HEALTHCHECK_TASK,
    PUSH_RETRY_TASK,
    build_beat_schedule,
    create_celery_app,
)
from open_source_intelligence.workers.tasks import pipeline_retry_countdown, resolve_business_date


def build_settings(
    *,
    schedule_enabled: bool = True,
    schedule_hour: int = 3,
    schedule_minute: int = 15,
    collect_limit: int = 7,
    backoff_seconds: int = 30,
) -> Settings:
    """为调度用例构造一套确定的 Worker 配置。"""
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://app:test@localhost:3306/intelligence",
        redis_url="redis://localhost:6379/15",
        secret_key="x" * 32,
        app_timezone="Asia/Shanghai",
        pipeline_schedule_enabled=schedule_enabled,
        pipeline_schedule_hour=schedule_hour,
        pipeline_schedule_minute=schedule_minute,
        pipeline_collect_limit=collect_limit,
        pipeline_retry_backoff_seconds=backoff_seconds,
    )


def test_beat_schedule_registers_the_daily_pipeline() -> None:
    """证明每日确定性流水线按配置的小时与分钟运行。"""
    schedule = build_beat_schedule(build_settings())

    assert set(schedule) == {
        "system-healthcheck",
        "daily-deterministic-pipeline",
        "publish-due-reports",
        "retry-failed-pushes",
    }
    assert schedule["system-healthcheck"]["task"] == HEALTHCHECK_TASK

    entry = schedule["daily-deterministic-pipeline"]
    assert entry["task"] == DAILY_PIPELINE_TASK
    assert entry["kwargs"] == {"provider": "all", "limit": 7}
    cron = entry["schedule"]
    assert isinstance(cron, crontab)
    assert cron.hour == {3}
    assert cron.minute == {15}


def test_beat_schedule_defaults_to_two_in_the_morning() -> None:
    """证明文档里承诺的默认值仍然是本地时间 02:00。"""
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://app:test@localhost:3306/intelligence",
        redis_url="redis://localhost:6379/15",
        secret_key="x" * 32,
    )

    cron = build_beat_schedule(settings)["daily-deterministic-pipeline"]["schedule"]

    assert isinstance(cron, crontab)
    assert cron.hour == {2}
    assert cron.minute == {0}


def test_beat_schedule_can_disable_the_daily_pipeline() -> None:
    """证明运维可以只关掉流水线调度，而让 Beat 继续运行。"""
    schedule = build_beat_schedule(build_settings(schedule_enabled=False))

    assert set(schedule) == {
        "system-healthcheck",
        "publish-due-reports",
        "retry-failed-pushes",
    }
    assert schedule["retry-failed-pushes"]["task"] == PUSH_RETRY_TASK


def test_daily_pipeline_task_is_registered() -> None:
    """证明调度表里的任务名确实指向一个已注册的 Celery 任务。"""
    app = create_celery_app(build_settings())

    assert DAILY_PIPELINE_TASK in app.tasks
    assert HEALTHCHECK_TASK in app.tasks
    assert DAILY_PIPELINE_TASK.endswith("tasks.run_daily_pipeline")


def test_retry_countdown_backs_off_exponentially() -> None:
    """证明每失败一次，重试前的等待就要更长。"""
    settings = build_settings(backoff_seconds=30)

    assert pipeline_retry_countdown(retries=0, settings=settings) == 30
    assert pipeline_retry_countdown(retries=1, settings=settings) == 60
    assert pipeline_retry_countdown(retries=2, settings=settings) == 120
    assert pipeline_retry_countdown(retries=0, settings=build_settings(backoff_seconds=0)) == 0


def test_resolve_business_date_uses_configured_timezone() -> None:
    """证明显式日期优先，否则「今天」取自配置的时区。"""
    settings = build_settings()

    assert resolve_business_date("2026-10-12", settings) == date(2026, 10, 12)
    expected = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    assert resolve_business_date(None, settings) == expected
