"""把 T6 的每日流水线经 Celery 派发出去，并报告执行结果。

成功模式证明：调度任务能被 worker 通过 broker 端到端跑完。重试模式证明：失败的那次尝试
会被记录下来并触发重试，且两次尝试都还能在 ``pipeline_runs`` 里查到。

运行方式为 `--mode success|retry --date YYYY-MM-DD`，脚本自己完成判据核对：成功模式下
状态必须是 `succeeded` 或 `partial`、首次尝试号为 1、阶段序为 collect → snapshot →
若干 rank；重试模式下必须恰好留下两轮全失败的尝试，错误码统一为 `COLLECTOR_FAILED`。
"""

import argparse
import json
from typing import Any

from sqlalchemy import func, select

from open_source_intelligence.core.config import get_settings
from open_source_intelligence.core.logging import redact_text
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.models.intelligence import PipelineRun
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.workers.celery_app import DAILY_PIPELINE_TASK, create_celery_app

BUSINESS_DATE_PATH = "$.business_date"


def parse_args() -> argparse.Namespace:
    """解析派发模式、目标日期与队列。"""
    parser = argparse.ArgumentParser(description="Dispatch the daily pipeline task")
    parser.add_argument("--mode", choices=("success", "retry"), required=True)
    parser.add_argument("--date", required=True, help="Business date in YYYY-MM-DD")
    parser.add_argument("--provider", choices=("github", "huggingface", "all"), default="github")
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--queue", default="celery")
    parser.add_argument("--timeout", type=float, default=180.0)
    return parser.parse_args()


def read_collect_attempts(business_date: str) -> list[dict[str, Any]]:
    """返回某个业务日期已记录的采集尝试。"""
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            runs = session.scalars(
                select(PipelineRun)
                .where(
                    PipelineRun.run_type == "collect",
                    func.json_extract(PipelineRun.metrics_json, BUSINESS_DATE_PATH)
                    == business_date,
                )
                .order_by(PipelineRun.attempt, PipelineRun.id)
            ).all()
            return [
                {
                    "run_id": run.id,
                    "attempt": run.attempt,
                    "status": run.status,
                    "error_code": run.error_code,
                }
                for run in runs
            ]
    finally:
        engine.dispose()


def main() -> None:
    """派发一次每日流水线任务，并核对它留下的执行结果。"""
    args = parse_args()
    app = create_celery_app()
    async_result = app.send_task(
        DAILY_PIPELINE_TASK,
        kwargs={
            "provider": args.provider,
            "limit": args.limit,
            "business_date": args.date,
        },
        queue=args.queue,
    )
    payload: dict[str, Any] = {
        "mode": args.mode,
        "task_id": async_result.id,
        "queue": args.queue,
        "business_date": args.date,
    }

    value: Any = None
    error: Exception | None = None
    try:
        value = async_result.get(timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001 - 重试模式下失败本身就是预期结果
        error = exc

    if args.mode == "success":
        if error is not None:
            raise RuntimeError(
                f"daily pipeline task failed: {type(error).__name__}: {error}"
            ) from error
        if not isinstance(value, dict):
            raise RuntimeError(f"unexpected task result: {value!r}")
        stages = value.get("stages") or []
        stage_types = [stage.get("run_type") for stage in stages]
        if value.get("status") != "succeeded" and value.get("status") != "partial":
            raise RuntimeError(f"unexpected pipeline status: {value.get('status')}")
        if value.get("attempt") != 1:
            raise RuntimeError(f"unexpected first attempt: {value.get('attempt')}")
        if stage_types[:2] != ["collect", "snapshot"] or any(
            stage_type != "rank" for stage_type in stage_types[2:]
        ):
            raise RuntimeError(
                f"expected collect, snapshot, then optional rank stages: {stage_types}"
            )
        if len(value.get("run_ids") or []) != len(stages):
            raise RuntimeError("run ids do not match the recorded stages")
        payload.update(
            {
                "outcome": "succeeded",
                "result_status": value.get("status"),
                "attempt": value.get("attempt"),
                "run_ids": value.get("run_ids"),
                "stage_types": stage_types,
                "rank_stages": len(stage_types) - 2,
                "provider_counts": value.get("provider_counts"),
                "snapshot_count": value.get("snapshot_count"),
            }
        )
    else:
        if error is None:
            raise RuntimeError(f"failing pipeline unexpectedly succeeded: {value!r}")
        attempts = read_collect_attempts(args.date)
        payload.update(
            {
                "outcome": "failed",
                "exception": type(error).__name__,
                "message": redact_text(str(error))[:300],
                "attempts": [item["attempt"] for item in attempts],
                "statuses": [item["status"] for item in attempts],
                "error_codes": [item["error_code"] for item in attempts],
                "run_ids": [item["run_id"] for item in attempts],
            }
        )
        if payload["attempts"] != [1, 2]:
            raise RuntimeError(f"expected two recorded attempts, got {attempts}")
        if set(payload["statuses"]) != {"failed"}:
            raise RuntimeError(f"expected every attempt to fail, got {payload['statuses']}")
        if set(payload["error_codes"]) != {"COLLECTOR_FAILED"}:
            raise RuntimeError(f"unexpected error codes: {payload['error_codes']}")

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
