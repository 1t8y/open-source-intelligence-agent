"""评测端点：数据集、执行、运行详情，以及对比报告。

路由顺序是有承重作用的。`/runs` 与 `/runs/{run_id}` 是字面量段，而
`/{dataset_id}/run` 与 `/{dataset_id}/comparison` 以路径参数开头，所以字面量
路由先声明。FastAPI 按声明顺序匹配，把两者调换就会让 `/runs` 被当成数据集
id 解析 —— 正是 `api/routes/push.py` 被拆出去要躲开的那个坑。

每个端点都要求写角色，与 `INTERFACES.md` §2 一致：一次评测运行会拿真实数据
去跑 Agent，因此它属于运维动作，而不是读操作。
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
    """一个数据集版本。"""

    id: int
    name: str
    version: str
    description: str | None
    created_by: int | None
    created_at: str


class DatasetListResponse(BaseModel):
    """已知的数据集版本，最新的在前。"""

    items: list[DatasetResponse]


class EvalCaseResponse(BaseModel):
    """一条数据集样本，不含参考答案。"""

    id: int
    case_key: str
    topic: str
    enabled: bool
    tags: list[str]


class EvalCaseListResponse(BaseModel):
    """某个数据集版本下的用例集合。"""

    items: list[EvalCaseResponse]


class EvalRunResponse(BaseModel):
    """某个目标在一个数据集版本上的一次执行。"""

    id: int
    dataset_id: int
    target_type: str
    status: str
    agent_config_id: int | None
    started_at: str | None
    finished_at: str | None
    summary: dict[str, Any] | None


class EvalRunListResponse(BaseModel):
    """最近的运行，最新的在前。"""

    items: list[EvalRunResponse]


class EvalResultResponse(BaseModel):
    """一次运行中某条用例的度量结果。"""

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
    """一次运行及其逐条用例的度量结果。"""

    results: list[EvalResultResponse]


class RunTargetsRequest(BaseModel):
    """要执行哪些变体；留空表示按报告顺序执行全部变体。"""

    targets: list[str] | None = Field(default=None, max_length=5)


class ComparisonResponse(BaseModel):
    """各变体之间统一的对比结果。"""

    dataset: dict[str, Any]
    case_count: int
    targets: list[dict[str, Any]]
    passed: bool
    regressions: list[str]
    failed_gates: dict[str, list[str]]


def _dataset_response(dataset: Any) -> DatasetResponse:
    """把一行数据集记录序列化成响应。"""
    return DatasetResponse(
        id=dataset.id,
        name=dataset.name,
        version=dataset.version,
        description=dataset.description,
        created_by=dataset.created_by,
        created_at=dataset.created_at.isoformat(),
    )


def _run_response(run: EvalRun) -> EvalRunResponse:
    """把一行运行记录序列化成响应。"""
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
    """序列化一份对比报告。"""
    return ComparisonResponse(**report.to_dict())


def _rate_text(value: Decimal | None) -> str | None:
    """把存储的比率渲染成纯文本；用例从未行使过该指标时返回 `None`。"""
    return None if value is None else str(value)


@router.get("", response_model=DatasetListResponse)
def get_datasets(
    user: WriteUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> DatasetListResponse:
    """返回已知的数据集版本。"""
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
    """返回最近的评测运行，让运维看清测了什么、什么时候测的。"""
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
    """返回一次运行及其逐条用例的度量结果。"""
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
    """在一个数据集上执行一个或多个目标变体，并返回对比结果。"""
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
    """返回各目标最近一次运行之间的对比，不重新跑任何东西。"""
    return _comparison_response(compare_latest(session, dataset_id=dataset_id))


@router.get("/{dataset_id}/cases", response_model=EvalCaseListResponse)
def get_dataset_cases(
    dataset_id: int,
    user: WriteUser,
    session: DbSession,
    enabled_only: Annotated[bool, Query()] = False,
) -> EvalCaseListResponse:
    """返回某个数据集版本下的用例。"""
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
