"""监控端点：看板背后的运营概览。

概览是纯读操作。它汇总的是流水线、Agent、报告与评测各层早就写好的台账，所以它能在不跑迁移的
前提下加进一个已在运行的部署，也不会打扰它只是在描述的那次运行。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from open_source_intelligence.core.config import Settings
from open_source_intelligence.observability.metrics import (
    DEFAULT_WINDOW_DAYS,
    MAX_WINDOW_DAYS,
    MIN_WINDOW_DAYS,
)
from open_source_intelligence.observability.queries import collect_metrics
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.security.authorization import METRICS_ROLES
from open_source_intelligence.security.dependencies import get_db_session, require_roles

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])

MetricsUser = Annotated[User, Depends(require_roles(*METRICS_ROLES))]
DbSession = Annotated[Session, Depends(get_db_session)]


def request_settings(request: Request) -> Settings:
    """返回绑定在这个应用实例上的配置。"""
    settings: Settings = request.app.state.settings
    return settings


class MetricsOverviewResponse(BaseModel):
    """看板的响应体：一个时间窗口加五个只读分区。"""

    window: dict[str, Any]
    pipeline: dict[str, Any]
    agent: dict[str, Any]
    tools: dict[str, Any]
    reports: dict[str, Any]
    evaluations: dict[str, Any]


@router.get("/overview", response_model=MetricsOverviewResponse)
def get_metrics_overview(
    user: MetricsUser,
    session: DbSession,
    now: Annotated[datetime, Depends(utc_now)],
    settings: Annotated[Settings, Depends(request_settings)],
    window_days: Annotated[
        int, Query(ge=MIN_WINDOW_DAYS, le=MAX_WINDOW_DAYS)
    ] = DEFAULT_WINDOW_DAYS,
) -> MetricsOverviewResponse:
    """返回一个时间窗口内的任务、token、耗时、失败与评测指标。

    `window_days` 按文档规定校验在 1-90 之间；运维实际会要的值是 1、7 和
    30。评测分区特意不按窗口切分 —— 原因见
    `observability.metrics.EvaluationMetrics`。
    """
    overview = collect_metrics(
        session,
        now=now,
        window_days=window_days,
        settings=settings,
    )
    return MetricsOverviewResponse(**overview.to_dict())
