"""Tests for the research Agent Celery task registration."""

from open_source_intelligence.core.config import Settings
from open_source_intelligence.workers import tasks as _tasks  # noqa: F401
from open_source_intelligence.workers.celery_app import (
    DAILY_PIPELINE_TASK,
    HEALTHCHECK_TASK,
    RESEARCH_AGENT_TASK,
    build_beat_schedule,
    create_celery_app,
)


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


def test_research_agent_task_is_registered() -> None:
    """The runtime is reachable from the worker through a named Celery task."""
    app = create_celery_app(build_settings())

    assert RESEARCH_AGENT_TASK in app.tasks
    assert RESEARCH_AGENT_TASK.endswith("tasks.run_research_agent")


def test_research_agent_is_not_scheduled_automatically() -> None:
    """The Agent spends real model budget, so Beat does not trigger it on its own.

    Automatic orchestration is wired once the report and publishing stages exist.
    """
    schedule = build_beat_schedule(build_settings())
    scheduled_tasks = {entry["task"] for entry in schedule.values()}

    assert set(schedule) == {"system-healthcheck", "daily-deterministic-pipeline"}
    assert scheduled_tasks == {HEALTHCHECK_TASK, DAILY_PIPELINE_TASK}
    assert RESEARCH_AGENT_TASK not in scheduled_tasks
