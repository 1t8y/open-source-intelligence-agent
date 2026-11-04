"""确定性流水线与 Worker 健康校验的 Celery 任务。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from celery import Task, shared_task
from sqlalchemy.orm import Session

from open_source_intelligence.agent.errors import AGENT_MODEL_UNAVAILABLE
from open_source_intelligence.agent.runtime import run_research_agent as execute_research_agent
from open_source_intelligence.backfill.errors import BackfillWalkFailed
from open_source_intelligence.backfill.jobs import DEFAULT_PER_PAGE, DEFAULT_TARGET_PAGES
from open_source_intelligence.backfill.service import resume_backfill as execute_resume_backfill
from open_source_intelligence.backfill.service import start_backfill as execute_start_backfill
from open_source_intelligence.collectors.cli import build_collectors, build_content_sources
from open_source_intelligence.collectors.service import collect_repositories
from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.notifications.service import PUSH_BATCH_LIMIT
from open_source_intelligence.notifications.service import (
    retry_failed_pushes as execute_retry_failed_pushes,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.pipeline.contents import business_day_start
from open_source_intelligence.pipeline.daily import run_daily_pipeline as execute_daily_pipeline
from open_source_intelligence.pipeline.research_dispatch import (
    ResearchTarget,
    dispatch_research_targets,
    select_research_targets,
)
from open_source_intelligence.pipeline.runs import PipelineStageError
from open_source_intelligence.reports.automation import finish_run_report
from open_source_intelligence.reports.publishing import (
    PUBLISH_BATCH_LIMIT,
)
from open_source_intelligence.reports.publishing import (
    publish_due_reports as execute_publish_due_reports,
)
from open_source_intelligence.workers.celery_app import (
    BACKFILL_SOFT_TIME_LIMIT_SECONDS,
    BACKFILL_TASK,
    BACKFILL_TIME_LIMIT_SECONDS,
    DAILY_PIPELINE_TASK,
    PUBLISH_DUE_TASK,
    PUSH_RETRY_TASK,
    RESEARCH_AGENT_TASK,
)


def resolve_business_date(value: str | None, settings: Settings) -> date:
    """解析显式的业务日期，或返回配置时区下的今天。"""
    if value:
        return date.fromisoformat(value)
    return datetime.now(ZoneInfo(settings.app_timezone)).date()


def pipeline_retry_countdown(*, retries: int, settings: Settings) -> int:
    """返回下一次流水线尝试前的指数退避秒数。"""
    # 1 << retries 就是 2**retries，且结果是 mypy 能跟上的 int。
    return settings.pipeline_retry_backoff_seconds * (1 << retries)


@shared_task(name="open_source_intelligence.workers.tasks.healthcheck")  # type: ignore[misc]
def healthcheck() -> dict[str, str]:
    """返回证明 Worker 确实执行过的稳定载荷。"""
    return {"status": "ok", "task": "healthcheck"}


@shared_task(name="open_source_intelligence.workers.tasks.collect_sources")  # type: ignore[misc]
def collect_sources(provider: str = "all", limit: int = 5) -> dict[str, object]:
    """采集一批仓库，并返回各供应商计数与错误。"""
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


@shared_task(bind=True, name=DAILY_PIPELINE_TASK)  # type: ignore[misc]
def run_daily_pipeline(
    self: Task,
    *,
    provider: str = "all",
    limit: int | None = None,
    business_date: str | None = None,
) -> dict[str, Any]:
    """执行一次采集、快照、排序与正文处理，并重试瞬时的阶段失败。

    每次尝试都写入自己的 PipelineRun 记录，因此重试轨迹保持可见：第 1 次失败、第 2 次成功，
    两次都可查询。

    当 `AGENT_AUTO_RESEARCH_ENABLED` 打开时，该批次随后会为每个符合条件的主题派发一次研究
    运行（见 `pipeline/research_dispatch.py`）。派发被刻意放在 `run_daily_pipeline` *之外*：
    那个函数是确定性的一半，会被演练和测试调用，而那些调用绝不能顺带排队模型请求。选择逻辑
    在同一个会话内运行，因此它能读到本批次刚写入的排序行。
    """
    if provider not in {"github", "huggingface", "all"}:
        raise ValueError("provider must be github, huggingface, or all")
    settings = get_settings()
    resolved_limit = settings.pipeline_collect_limit if limit is None else limit
    if not 1 <= resolved_limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    attempt = self.request.retries + 1
    resolved_date = resolve_business_date(business_date, settings)
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    dispatch_payload: dict[str, Any] | None = None
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            collectors = build_collectors(provider, client, github_sort="updated")
            content_sources = build_content_sources(provider, client)
            with session_factory() as session:
                result = execute_daily_pipeline(
                    session,
                    business_date=resolved_date,
                    collectors=collectors,
                    limit=resolved_limit,
                    content_sources=content_sources,
                    content_limit=settings.pipeline_content_limit,
                    content_refresh_after=business_day_start(resolved_date, settings.app_timezone),
                    attempt=attempt,
                )
                if settings.agent_auto_research_enabled:
                    dispatch_payload = _dispatch_research(
                        session,
                        business_date=resolved_date,
                        settings=settings,
                        app=self.app,
                    )
    except PipelineStageError as exc:
        if self.request.retries < settings.pipeline_task_max_retries:
            raise self.retry(
                exc=exc,
                countdown=pipeline_retry_countdown(
                    retries=self.request.retries,
                    settings=settings,
                ),
            ) from exc
        raise
    finally:
        engine.dispose()
    payload: dict[str, Any] = result.to_dict()
    payload["task"] = DAILY_PIPELINE_TASK
    # 开关关闭时用 `None` 而不是 `{}`：「我们没去看」与「我们看了但没找到」是两件不同的
    # 事实，而把两者都渲染成空字典的载荷，会替后来读结果的人抹掉这个区别。
    payload["research_dispatched"] = dispatch_payload
    return payload


def _dispatch_research(
    session: Session,
    *,
    business_date: date,
    settings: Settings,
    app: Any,
) -> dict[str, Any]:
    """选出今天的研究目标，并为每个主题排队一个任务。

    按名称调用 `app.send_task`，而不是导入绑定任务再调 `.delay`：日常任务不能在调用时依赖
    研究模块可导入（它会拉起模型适配器），而按名称发送才能让任意进程上的 worker 接手它。
    """
    selection = select_research_targets(
        session,
        business_date=business_date,
        timezone_name=settings.app_timezone,
        limit=settings.agent_daily_research_limit,
    )

    def enqueue(target: ResearchTarget) -> str:
        kwargs: dict[str, Any] = {"topic_id": target.topic_id}
        if target.window_start is not None:
            kwargs["window_start"] = target.window_start.isoformat()
        async_result = app.send_task(RESEARCH_AGENT_TASK, kwargs=kwargs)
        return str(async_result.id)

    report = dispatch_research_targets(selection.targets, enqueue)
    return {
        **report.to_dict(),
        "eligible": selection.eligible_count,
        "deferred": selection.deferred_count,
        "already_started": selection.already_started_topic_ids,
    }


def _optional_datetime(value: str | None) -> datetime | None:
    """从任务载荷中解析一个可选的 ISO 8601 时间戳。"""
    return None if value is None else datetime.fromisoformat(value)


@shared_task(bind=True, name=RESEARCH_AGENT_TASK)  # type: ignore[misc]
def run_research_agent(
    self: Task,
    *,
    topic_id: int,
    window_start: str | None = None,
    window_end: str | None = None,
    max_candidates: int | None = None,
) -> dict[str, Any]:
    """为一个主题运行一次有界的研究 Agent。

    瞬时的模型传输失败会按共享的流水线退避重试。其他任何结果都原样返回，因为一份待评审的
    草稿是结果而不是错误：只有模型的传输问题才值得重试。
    """
    if topic_id <= 0:
        raise ValueError("topic_id must be positive")
    if max_candidates is not None and max_candidates <= 0:
        raise ValueError("max_candidates must be positive")
    settings = get_settings()
    if not settings.agent_enabled:
        raise ValueError("the research Agent is disabled by AGENT_ENABLED")
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    report_summary: dict[str, Any] | None = None
    try:
        with session_factory() as session:
            result = execute_research_agent(
                session,
                topic_id=topic_id,
                settings=settings,
                window_start=_optional_datetime(window_start),
                window_end=_optional_datetime(window_end),
                max_candidates=max_candidates,
            )
            # 草稿本身不是报告：缺了这条边，日常链路就停在 `report_drafts`，
            # 于是没有任何东西被校验、没有任何东西被发布、也没有邮件发出。
            if settings.agent_auto_report_enabled and result.report_draft_id is not None:
                report_summary = finish_run_report(session, agent_run_id=result.agent_run_id)
    finally:
        engine.dispose()
    if (
        result.status == "failed"
        and result.error_code == AGENT_MODEL_UNAVAILABLE
        and self.request.retries < settings.pipeline_task_max_retries
    ):
        raise self.retry(
            countdown=pipeline_retry_countdown(
                retries=self.request.retries,
                settings=settings,
            ),
        )
    payload: dict[str, Any] = result.to_dict()
    payload["report"] = report_summary
    payload["task"] = RESEARCH_AGENT_TASK
    return payload


@shared_task(name=PUBLISH_DUE_TASK)  # type: ignore[misc]
def publish_due_reports(limit: int = PUBLISH_BATCH_LIMIT) -> dict[str, Any]:
    """释放撤回窗口已到期的报告。

    清扫是幂等的：报告只有在处于 `scheduled` 且已过 `publish_at` 时才会被释放，因此重复
    运行会发现无事可做。被判定违反发布策略的报告会带着稳定错误码被标记为 `failed`，而不是
    中止整批。
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            released = execute_publish_due_reports(session, limit=limit)
    finally:
        engine.dispose()
    published_count = sum(1 for item in released if item["status"] == "published")
    return {
        "status": "ok",
        "task": PUBLISH_DUE_TASK,
        "released_count": published_count,
        "blocked_count": len(released) - published_count,
        "released": released,
    }


@shared_task(name=PUSH_RETRY_TASK)  # type: ignore[misc]
def retry_failed_pushes(limit: int = PUSH_BATCH_LIMIT) -> dict[str, Any]:
    """重试那些此前被传输层拒收的报告推送。

    在这里重试而不是放进发布清扫，让两件事彼此分开：一次到期的发布不必排在邮件服务器后面，
    而一次失败的投递必须按自己的节奏重试。清扫是幂等的，因为推送行只有在处于 `failed` 且
    仍在重试预算内时才会保持为候选。
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            outcomes = execute_retry_failed_pushes(session, limit=limit, settings=settings)
    finally:
        engine.dispose()
    delivered = [item for item in outcomes if item.delivered]
    return {
        "status": "ok",
        "task": PUSH_RETRY_TASK,
        "delivered_count": len(delivered),
        "failed_count": len(outcomes) - len(delivered),
        "attempted_count": len(outcomes),
        "outcomes": [item.to_dict() for item in outcomes],
    }


@shared_task(  # type: ignore[misc]
    bind=True,
    name=BACKFILL_TASK,
    time_limit=BACKFILL_TIME_LIMIT_SECONDS,
    soft_time_limit=BACKFILL_SOFT_TIME_LIMIT_SECONDS,
)
def run_backfill(
    self: Task,
    *,
    provider: str = "github",
    target_pages: int = DEFAULT_TARGET_PAGES,
    per_page: int = DEFAULT_PER_PAGE,
    job_id: int | None = None,
) -> dict[str, Any]:
    """走完某个供应商的分页历史，并从它停下的游标处重试。

    重试会续跑同一个作业，而不是新建一个，这正是逐页持久化游标的全部回报：一次死在第 7 页
    （共 20 页）的尝试只损失第 7 页，而一次另开作业的重试会把同样的限流预算又花在重读第 1
    到第 6 页上。

    `job_id` 只在重试时被设置。它存在时 `target_pages` 与 `per_page` 会被忽略，因为作业行
    已经携带了它们 —— 重试绝不能放宽操作员选定的预算。
    """
    if provider not in {"github", "huggingface"}:
        raise ValueError("provider must be github or huggingface")
    if job_id is not None and job_id <= 0:
        raise ValueError("job_id must be positive")
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            collector = build_collectors(provider, client)[0]
            with session_factory() as session:
                try:
                    if job_id is None:
                        outcome = execute_start_backfill(
                            session,
                            collector=collector,
                            target_pages=target_pages,
                            per_page=per_page,
                        )
                    else:
                        outcome = execute_resume_backfill(
                            session,
                            job_id=job_id,
                            collector=collector,
                        )
                except BackfillWalkFailed as exc:
                    if self.request.retries < settings.pipeline_task_max_retries:
                        raise self.retry(
                            exc=exc,
                            countdown=pipeline_retry_countdown(
                                retries=self.request.retries,
                                settings=settings,
                            ),
                            kwargs={
                                "provider": provider,
                                "target_pages": target_pages,
                                "per_page": per_page,
                                "job_id": exc.job_id,
                            },
                        ) from exc
                    raise
    finally:
        engine.dispose()
    payload: dict[str, Any] = outcome.to_dict()
    payload["task"] = BACKFILL_TASK
    return payload
