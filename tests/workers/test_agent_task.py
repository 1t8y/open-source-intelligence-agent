"""守护研究 Agent 的 Celery 任务注册。"""

from open_source_intelligence.core.config import Settings
from open_source_intelligence.workers import tasks as _tasks  # noqa: F401
from open_source_intelligence.workers.celery_app import (
    DAILY_PIPELINE_TASK,
    HEALTHCHECK_TASK,
    PUBLISH_DUE_TASK,
    PUSH_RETRY_TASK,
    RESEARCH_AGENT_TASK,
    build_beat_schedule,
    create_celery_app,
)


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


def test_research_agent_task_is_registered() -> None:
    """证明 Worker 能通过一个有名字的 Celery 任务触达运行时。"""
    app = create_celery_app(build_settings())

    assert RESEARCH_AGENT_TASK in app.tasks
    assert RESEARCH_AGENT_TASK.endswith("tasks.run_research_agent")


def test_research_agent_is_not_scheduled_automatically() -> None:
    """证明 Beat 不会自己去触发 Agent，因为它要烧真实的模型预算。

    自动编排要等报告与发布阶段就位后才接线。发布清扫与推送重试清扫不受此限，
    因为两者都不花模型预算。
    """
    schedule = build_beat_schedule(build_settings())
    scheduled_tasks = {entry["task"] for entry in schedule.values()}

    assert set(schedule) == {
        "system-healthcheck",
        "daily-deterministic-pipeline",
        "publish-due-reports",
        "retry-failed-pushes",
    }
    assert scheduled_tasks == {
        HEALTHCHECK_TASK,
        DAILY_PIPELINE_TASK,
        PUBLISH_DUE_TASK,
        PUSH_RETRY_TASK,
    }
    assert RESEARCH_AGENT_TASK not in scheduled_tasks
