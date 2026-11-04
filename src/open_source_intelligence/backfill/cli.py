"""可续跑历史回填的命令行入口。

动作集合对应的是作业生命周期，而不是 HTTP 接口：`start` 创建并走查一个
作业，`resume` 从游标处继续一个作业，`status` / `list` 只做报告、不碰
源。
"""

import argparse
import json
from typing import Any

import httpx

from open_source_intelligence.backfill.errors import (
    BackfillJobBusy,
    BackfillLimitError,
    BackfillNotResumable,
    BackfillUnsupportedCollector,
    BackfillWalkFailed,
)
from open_source_intelligence.backfill.jobs import (
    DEFAULT_PER_PAGE,
    DEFAULT_TARGET_PAGES,
    MAX_TARGET_PAGES,
    get_job,
    list_jobs,
    summarize_job,
    summarize_jobs,
)
from open_source_intelligence.backfill.service import resume_backfill, start_backfill
from open_source_intelligence.collectors.cli import build_collectors
from open_source_intelligence.collectors.limiter import RateLimiter
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)


def parse_args() -> argparse.Namespace:
    """解析回填动作与它的过滤条件。"""
    parser = argparse.ArgumentParser(
        description="Walk a provider's paged search history into the repository catalog"
    )
    parser.add_argument(
        "--action",
        choices=("start", "resume", "status", "list"),
        default="start",
    )
    parser.add_argument(
        "--provider",
        choices=("github", "huggingface"),
        default="github",
        help="A walk covers one source, so there is no 'all'",
    )
    parser.add_argument(
        "--target-pages",
        type=int,
        default=DEFAULT_TARGET_PAGES,
        help=f"Page budget for a new job, 1 to {MAX_TARGET_PAGES}",
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=DEFAULT_PER_PAGE,
        help="Repositories requested per page",
    )
    parser.add_argument("--job-id", type=int, help="The job to resume or inspect")
    parser.add_argument(
        "--requests-per-minute",
        type=int,
        help="Override the data source's own rate limit for this walk",
    )
    parser.add_argument("--job-status", help="Filter the job list by status")
    parser.add_argument("--limit", type=int, default=20, help="Job list page size")
    return parser.parse_args()


def _limiter(args: argparse.Namespace) -> RateLimiter | None:
    """只有当操作者覆盖了源自身的速率时，才构造一个显式限速器。"""
    if args.requests_per_minute is None:
        return None
    return RateLimiter(per_minute=args.requests_per_minute)


def _read(args: argparse.Namespace) -> dict[str, Any]:
    """在不接触供应商的情况下回答 `status` 与 `list`。"""
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            if args.action == "status":
                if args.job_id is None:
                    raise SystemExit("--job-id is required for --action status")
                job = get_job(session, args.job_id)
                return {"job": None if job is None else summarize_job(job).to_dict()}
            jobs = list_jobs(
                session,
                provider=args.provider,
                status=args.job_status,
                limit=args.limit,
            )
            return {"jobs": summarize_jobs(jobs)}
    finally:
        engine.dispose()


def _walk(args: argparse.Namespace) -> dict[str, Any]:
    """针对真实供应商运行或续跑一次走查。"""
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            collector = build_collectors(args.provider, client)[0]
            with session_factory() as session:
                if args.action == "resume":
                    if args.job_id is None:
                        raise SystemExit("--job-id is required for --action resume")
                    outcome = resume_backfill(
                        session,
                        job_id=args.job_id,
                        collector=collector,
                        limiter=_limiter(args),
                    )
                else:
                    outcome = start_backfill(
                        session,
                        collector=collector,
                        target_pages=args.target_pages,
                        per_page=args.per_page,
                        limiter=_limiter(args),
                    )
                return outcome.to_dict()
    finally:
        engine.dispose()


def main() -> None:
    """执行请求的回填操作并打印 JSON 载荷。"""
    args = parse_args()
    if args.action in {"status", "list"}:
        print(json.dumps(_read(args), ensure_ascii=False, sort_keys=True))
        return
    try:
        payload = _walk(args)
    except (
        BackfillNotResumable,
        BackfillJobBusy,
        BackfillLimitError,
        BackfillUnsupportedCollector,
    ) as exc:
        # 这些属于操作者的失误，而不是走查的结果：什么都没跑，也就没有作业
        # 载荷可打印，消息本身就是全部答案。
        raise SystemExit(str(exc)) from exc
    except BackfillWalkFailed as exc:
        # 一次失败的走查最有价值的输出是游标停在哪里：作业仍可续跑，所以载
        # 荷是操作者的下一条命令，而不是一段 traceback。
        settings = get_settings()
        engine = create_database_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        try:
            with session_factory() as session:
                failed = get_job(session, exc.job_id)
                summary = None if failed is None else summarize_job(failed).to_dict()
        finally:
            engine.dispose()
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": exc.error_code,
                    "message": str(exc),
                    "job": summary,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        raise SystemExit(1) from exc
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
