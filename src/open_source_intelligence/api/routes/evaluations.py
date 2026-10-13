"""Evaluation endpoints: datasets, execution, run detail, and the comparison report.

Route order is load-bearing. `/runs` and `/runs/{run_id}` are literal segments while
`/{dataset_id}/run` and `/{dataset_id}/comparison` start with a path parameter, so the literal
routes are declared first. FastAPI matches in declaration order, and reversing them would make
`/runs` parse as a dataset id - exactly the trap `api/routes/push.py` was split out to avoid.

Every endpoint requires a write role, matching `INTERFACES.md` §2: an evaluation run executes
the Agent against real data, so it is an operator action rather than a read.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.evaluation.dataset import list_cases, list_datasets, resolve_dataset
from open_source_intelligence.evaluation.errors import EvaluationNotFound
from open_source_intelligence.evaluation.runner import (
    ComparisonReport,
    compare_latest,
    list_runs,
    run_evaluation,
)
from open_source_intelligence.persistence.models.evaluation import EvalCase, EvalResult, EvalRun
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.security.authorization import REPORT_WRITE_ROLES
from open_source_intelligence.security.dependencies import get_db_session, require_roles

router = APIRouter(prefix="/api/v1/evaluations", tags=["evaluations"])

WriteUser = Annotated[User, Depends(require_roles(*REPORT_WRITE_ROLES))]
DbSession = Annotated[Session, Depends(get_db_session)]


class DatasetResponse(BaseModel):
    """One dataset version."""

    id: int
    name: str
    version: str
    description: str | None
    created_by: int | None
    created_at: str


class DatasetListResponse(BaseModel):
    """Known dataset versions, newest first."""

    items: list[DatasetResponse]


class EvalCaseResponse(BaseModel):
    """One dataset sample, without its reference answer."""

    id: int
    case_key: str
    topic: str
    enabled: bool
    tags: list[str]


class EvalCaseListResponse(BaseModel):
    """The cases of one dataset version."""

    items: list[EvalCaseResponse]


class EvalRunResponse(BaseModel):
    """One target's execution against one dataset version."""

    id: int
    dataset_id: int
    target_type: str
    status: str
    agent_config_id: int | None
    started_at: str | None
    finished_at: str | None
    summary: dict[str, Any] | None


class EvalRunListResponse(BaseModel):
    """Recent runs, newest first."""

    items: list[EvalRunResponse]


class EvalResultResponse(BaseModel):
    """One case's measurement inside a run."""

    id: int
    eval_case_id: int
    case_key: str
    status: str
    citation_coverage: str | None
    factual_consistency: str | None
    task_completion: str | None
    cost: str
    latency_ms: int | None
    human_score: int | None
    details: dict[str, Any] | None


class EvalRunDetailResponse(EvalRunResponse):
    """A run with its per-case measurements."""

    results: list[EvalResultResponse]


class RunTargetsRequest(BaseModel):
    """Which variants to execute; omitted means every variant, in report order."""

    targets: list[str] | None = Field(default=None, max_length=5)


class ComparisonResponse(BaseModel):
    """The unified comparison across variants."""

    dataset: dict[str, Any]
    case_count: int
    targets: list[dict[str, Any]]
    passed: bool
    regressions: list[str]
    failed_gates: dict[str, list[str]]


def _dataset_response(dataset: Any) -> DatasetResponse:
    """Serialize one dataset row."""
    return DatasetResponse(
        id=dataset.id,
        name=dataset.name,
        version=dataset.version,
        description=dataset.description,
        created_by=dataset.created_by,
        created_at=dataset.created_at.isoformat(),
    )


def _run_response(run: EvalRun) -> EvalRunResponse:
    """Serialize one run row."""
    return EvalRunResponse(
        id=run.id,
        dataset_id=run.dataset_id,
        target_type=run.target_type,
        status=run.status,
        agent_config_id=run.agent_config_id,
        started_at=run.started_at.isoformat() if run.started_at else None,
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
        summary=run.summary_json,
    )


def _comparison_response(report: ComparisonReport) -> ComparisonResponse:
    """Serialize a comparison report."""
    return ComparisonResponse(**report.to_dict())


def _rate_text(value: Decimal | None) -> str | None:
    """Render a stored rate as a plain string, or None when the case never exercised it."""
    return None if value is None else str(value)


@router.get("", response_model=DatasetListResponse)
def get_datasets(
    user: WriteUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> DatasetListResponse:
    """Return the known dataset versions."""
    rows = list_datasets(session, limit=limit)
    return DatasetListResponse(items=[_dataset_response(row) for row in rows])


@router.get("/runs", response_model=EvalRunListResponse)
def get_eval_runs(
    user: WriteUser,
    session: DbSession,
    dataset_id: Annotated[int | None, Query(ge=1)] = None,
    target_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> EvalRunListResponse:
    """Return recent evaluation runs so an operator can see what was measured and when."""
    rows = list_runs(
        session,
        dataset_id=dataset_id,
        target_type=target_type,
        limit=limit,
    )
    return EvalRunListResponse(items=[_run_response(row) for row in rows])


@router.get("/runs/{run_id}", response_model=EvalRunDetailResponse)
def get_eval_run(
    run_id: int,
    user: WriteUser,
    session: DbSession,
) -> EvalRunDetailResponse:
    """Return one run with its per-case measurements."""
    run = session.get(EvalRun, run_id)
    if run is None:
        raise EvaluationNotFound(f"eval run {run_id} does not exist")
    rows = session.scalars(
        select(EvalResult).where(EvalResult.eval_run_id == run_id).order_by(EvalResult.id.asc())
    ).all()
    case_keys = {
        case.id: case.case_key
        for case in session.scalars(
            select(EvalCase).where(EvalCase.id.in_([row.eval_case_id for row in rows]))
        )
    }
    base = _run_response(run)
    return EvalRunDetailResponse(
        **base.model_dump(),
        results=[
            EvalResultResponse(
                id=row.id,
                eval_case_id=row.eval_case_id,
                case_key=case_keys.get(row.eval_case_id, ""),
                status=row.status,
                citation_coverage=_rate_text(row.citation_coverage),
                factual_consistency=_rate_text(row.factual_consistency),
                task_completion=_rate_text(row.task_completion),
                cost=str(row.cost),
                latency_ms=row.latency_ms,
                human_score=row.human_score,
                details=row.details_json,
            )
            for row in rows
        ],
    )


@router.post("/{dataset_id}/run", response_model=ComparisonResponse)
def run_dataset_evaluation(
    dataset_id: int,
    user: WriteUser,
    session: DbSession,
    payload: RunTargetsRequest | None = None,
) -> ComparisonResponse:
    """Execute one or more target variants over a dataset and return the comparison."""
    report = run_evaluation(
        session,
        dataset_id=dataset_id,
        target_types=None if payload is None else payload.targets,
    )
    return _comparison_response(report)


@router.get("/{dataset_id}/comparison", response_model=ComparisonResponse)
def get_dataset_comparison(
    dataset_id: int,
    user: WriteUser,
    session: DbSession,
) -> ComparisonResponse:
    """Return the comparison over the newest run of each target, without re-running anything."""
    return _comparison_response(compare_latest(session, dataset_id=dataset_id))


@router.get("/{dataset_id}/cases", response_model=EvalCaseListResponse)
def get_dataset_cases(
    dataset_id: int,
    user: WriteUser,
    session: DbSession,
    enabled_only: Annotated[bool, Query()] = False,
) -> EvalCaseListResponse:
    """Return a dataset version's cases."""
    dataset = resolve_dataset(session, dataset_id=dataset_id)
    rows = list_cases(session, dataset_id=dataset.id, enabled_only=enabled_only)
    return EvalCaseListResponse(
        items=[
            EvalCaseResponse(
                id=row.id,
                case_key=row.case_key,
                topic=str((row.input_json or {}).get("topic", "")),
                enabled=row.enabled,
                tags=[
                    str(item)
                    for item in (row.input_json or {}).get("tags", [])
                    if isinstance(item, str)
                ],
            )
            for row in rows
        ]
    )
