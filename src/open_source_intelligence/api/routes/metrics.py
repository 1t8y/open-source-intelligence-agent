"""Monitoring endpoint: the operational overview behind the dashboard.

The overview is a pure read. It aggregates the ledger the pipeline, the Agent, the report and
the evaluation layers already wrote, which is why it could be added to a running deployment
without a migration and cannot disturb a run it is only describing.
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
    """Return the settings bound to this application instance."""
    settings: Settings = request.app.state.settings
    return settings


class MetricsOverviewResponse(BaseModel):
    """The dashboard payload: one window plus five read-only sections."""

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
    """Return task, token, latency, failure and evaluation metrics for one time window.

    `window_days` is validated to the documented 1-90 range; the values an operator actually
    asks for are 1, 7 and 30. The evaluation section is deliberately not windowed - see
    `observability.metrics.EvaluationMetrics`.
    """
    overview = collect_metrics(
        session,
        now=now,
        window_days=window_days,
        settings=settings,
    )
    return MetricsOverviewResponse(**overview.to_dict())
