"""Celery 应用工厂与共享运行时配置。"""

from datetime import timedelta
from typing import Any

from celery import Celery
from celery.schedules import crontab

from open_source_intelligence.core.config import Settings, get_settings

HEALTHCHECK_TASK = "open_source_intelligence.workers.tasks.healthcheck"
DAILY_PIPELINE_TASK = "open_source_intelligence.workers.tasks.run_daily_pipeline"
RESEARCH_AGENT_TASK = "open_source_intelligence.workers.tasks.run_research_agent"
PUBLISH_DUE_TASK = "open_source_intelligence.workers.tasks.publish_due_reports"
PUSH_RETRY_TASK = "open_source_intelligence.workers.tasks.retry_failed_pushes"
BACKFILL_TASK = "open_source_intelligence.workers.tasks.run_backfill"

BACKFILL_TIME_LIMIT_SECONDS = 1800
BACKFILL_SOFT_TIME_LIMIT_SECONDS = 1740
"""带节流的走查会超出 Worker 默认的五分钟上限，这是刻意的。

真正约束它的是自身的边界：`MAX_TARGET_PAGES` 页，每一页都受数据源配置的请求预算限制。
默认每分钟 30 次请求时，一百页光排队就超过三分钟，还没算任何请求延迟；把数据源配到每分钟
10 次，则需要十分钟。在第五分钟杀掉走查，会中断一个完全按配置正常工作的作业 —— 作业可续跑，
所以还活得下来，但这一次尝试已经注定跑不完，却把限流预算烧掉了。"""


def build_beat_schedule(settings: Settings) -> dict[str, dict[str, Any]]:
    """返回流水线、发布清扫与推送重试的周期调度。

    发布清扫始终开启：释放已过撤回窗口的报告不消耗任何模型预算，把它留作手动操作会毁掉
    `AUTO_WITH_DELAY` 的意义。推送重试单独调度，这样一台缓慢或不可用的邮件服务器无法拖延
    一次已经到期的发布。

    回填被刻意排除在外。它是操作员针对共享公共 API 的一次有意、一次性、昂贵的动作，所以
    按需派发，而不是交给一个会去翻没人要求的历史的时钟。
    """
    schedule: dict[str, dict[str, Any]] = {
        "system-healthcheck": {
            "task": HEALTHCHECK_TASK,
            "schedule": 60.0,
        },
        "publish-due-reports": {
            "task": PUBLISH_DUE_TASK,
            "schedule": timedelta(minutes=settings.publish_sweep_minutes),
        },
        "retry-failed-pushes": {
            "task": PUSH_RETRY_TASK,
            "schedule": timedelta(minutes=settings.push_retry_interval_minutes),
        },
    }
    if settings.pipeline_schedule_enabled:
        schedule["daily-deterministic-pipeline"] = {
            "task": DAILY_PIPELINE_TASK,
            "schedule": crontab(
                hour=settings.pipeline_schedule_hour,
                minute=settings.pipeline_schedule_minute,
            ),
            "kwargs": {
                "provider": "all",
                "limit": settings.pipeline_collect_limit,
            },
        }
    return schedule


def create_celery_app(settings: Settings | None = None) -> Celery:
    """创建并配置 Celery 应用，且不连接 Redis。"""
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
        beat_schedule=build_beat_schedule(resolved_settings),
    )
    return app
