"""针对 MySQL 跑 T15 的可观测性冒烟。

脚本在它自己种下的数据库上（而不是内存 SQLite 上）驱动*真正的*聚合层、*真正的*
JSON 端点与*真正的*服务端渲染看板。它端到端覆盖 T15 的验收标准：

1. **任务、Token、延迟、失败率可见** —— 总览从运行账本报出流水线吞吐、Agent 的
   Token 花销与两个延迟分位数，而且每个数都与对同一批行的新聚合结果吻合。
2. **评测结果可见** —— 最近一次评估清扫被原样复用，所以看板上的结论就是
   `compare_latest` 给出的结论，而不是第二套实现。
3. **窗口是可调且有界的** —— `1 / 7 / 30` 天窗口各自只报落在窗内的行，越界值会被
   夹到边界，零或负数则被拒绝。
4. **未行使的指标是 `None` 而不是 0** —— 一个没有任何 Agent 运行的窗口根本不报
   成功率，这与一个全是真实失败的窗口读起来是不同的。
5. **能从失败运行追到具体工具调用** —— 轨迹端点与轨迹页都能追到失败的那一步、
   被拒绝的那个工具，以及它的错误码。
6. **看板与轨迹页只对 ADMIN / EDITOR 开放** —— `VIEWER` 在 API 与界面上都被拒，
   导航项也不会为他们渲染。
7. **出口只有一条脱敏规则** —— 结果摘要里凭据形状的字符串，在 JSON 响应与渲染后
   的页面里都会被脱敏，因为界面也是一条出口路径。

有两条设计说明值得直说而不是藏起来。窗口锚定在运行时的墙上时钟（`utc_now()`），
每一行种下的数据都放在相对它固定的*偏移*上，因此断言在任何一天都成立，7 天窗口
也确实意味着 7 天。另外，种下的世界里包含一块 40 天前的行：没有它，「窗口会过滤」
这件事在一份根本不过滤的查询上也能通过。

每次断言都经一个*全新的*会话读取，所以通过的检查不可能是靠写行那个会话的身份映射
解释掉的。
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.runtime import load_or_create_config
from open_source_intelligence.api.app import create_app
from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.evaluation.dataset import import_dataset, parse_dataset
from open_source_intelligence.evaluation.runner import compare_latest, run_evaluation
from open_source_intelligence.evaluation.targets import AGENT_TARGET, BASELINE_TARGET
from open_source_intelligence.observability.metrics import (
    AGENT_OUTCOME_STATUSES,
    AGENT_SUCCEEDED_STATUSES,
    MANUAL_BLOCK_STATUS,
    PIPELINE_OUTCOME_STATUSES,
    PIPELINE_SUCCEEDED_STATUSES,
    TOOL_SUCCEEDED_STATUSES,
)
from open_source_intelligence.observability.queries import (
    collect_metrics,
    get_agent_run_trace,
    list_agent_runs,
)
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.agent import AgentRun, AgentStep, ToolCall
from open_source_intelligence.persistence.models.evaluation import (
    EvalCase,
    EvalDataset,
    EvalResult,
    EvalRun,
)
from open_source_intelligence.persistence.models.identity import User, UserRole
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    PipelineRun,
    Repository,
    RepositoryContent,
    RepoSnapshot,
    Topic,
    TopicRepository,
)
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.security.passwords import hash_password
from open_source_intelligence.security.service import create_user

METRICS_API = "/api/v1/metrics/overview"
AGENT_API = "/api/v1/agent/runs"
DASHBOARD = "/ui/dashboard"

SMOKE_PASSWORD = "t15-smoke-password-not-for-production"
ACTOR_PREFIX = "__t15_"
ROLES = {"admin": "ADMIN", "editor": "EDITOR", "viewer": "VIEWER"}

#: 这些可观测性行所挂靠的主题，同时也是评估用例点名的主题。
TOPIC_NAME = "__T15 Topic__"
DATASET_NAME = "__t15-observability__"
DATASET_VERSION = "v1"
REPORT_TITLE_PREFIX = "__T15 report__"
PROVIDER_PREFIX = "__t15_smoke_"
CONFIG_PREFIX = "__t15_"

#: 一个凭据形状的值，绝不允许经任何一条出口路径到达客户端。
LEAKED_SECRET = "sk-Abcdefghij0123456789"
DENIED_TOOL = "get_repository_content"
DENIED_ERROR = "TOOL_NOT_ALLOWED"
FAILED_RUN_ERROR = "AGENT_TOOL_BUDGET_EXCEEDED"
UNLINKED_TOOL_ERROR = "TOOL_TIMEOUT"

#: 各行相对运行时钟的偏移。`RECENT` 落在 1、7、30 天窗口内；`PRIOR_REPORT` 落在
#: 30 与 90 天内、7 天外；`STALE` 落在 30 天外、只在 90 天内。正是这三条互不相同的
#: 带，才让窗口断言有了意义。
RECENT = timedelta(days=3)
PRIOR_REPORT = timedelta(days=9)
STALE = timedelta(days=40)

#: 评估用例窗口内的两个快照点，与种下的计数对得上。
FIRST_SNAPSHOT = timedelta(days=9)
LAST_SNAPSHOT = timedelta(days=4)

ALPHA_STARS = 1240
ALPHA_FORKS = 210
BETA_STARS = 880
BETA_FORKS = 96

#: 7 天窗口内的 Agent 运行行：(状态, publish_mode, 错误码, token, 成本, 毫秒)。
AGENT_ROWS: tuple[tuple[str, str | None, str | None, int, int, str, int], ...] = (
    ("completed", "AUTO_WITH_DELAY", None, 1200, 300, "0.012300", 40_000),
    ("needs_review", "MANUAL", None, 900, 200, "0.008000", 60_000),
    ("failed", None, FAILED_RUN_ERROR, 400, 50, "0", 80_000),
)
#: 7 天窗口内的流水线行：(run_type, 状态, 毫秒)。
PIPELINE_ROWS: tuple[tuple[str, str, int], ...] = (
    ("collect", "succeeded", 10_000),
    ("snapshot", "succeeded", 20_000),
    ("rank", "failed", 30_000),
    ("agent", "partial", 40_000),
)


def _count(session: Session, entity: Any) -> int:
    """返回单张表的行数。"""
    return int(session.scalar(select(func.count()).select_from(entity)) or 0)


def _refresh(session: Session) -> None:
    """另一个会话写过库之后，丢弃本地缓存的行。"""
    session.rollback()
    session.expire_all()


# --------------------------------------------------------------------------------------
# 播种与清理
# --------------------------------------------------------------------------------------


def _actor_ids(session: Session) -> dict[str, int]:
    """返回 T15 冒烟用户的 id；缺失的当场建出来。"""
    found: dict[str, int] = {}
    for suffix, role in ROLES.items():
        username = f"{ACTOR_PREFIX}{suffix}__"
        existing = session.scalar(select(User.id).where(User.username == username))
        if existing is not None:
            found[suffix] = int(existing)
            continue
        user = create_user(
            session,
            username=username,
            password_hash=hash_password(SMOKE_PASSWORD),
            display_name=f"T15 {suffix}",
            role_codes={role},
            data_scope="team",
        )
        found[suffix] = int(user.id)
    session.commit()
    return found


def _t15_dataset_ids(session: Session) -> list[int]:
    """返回上一轮或本轮 T15 运行建出的数据集。"""
    return [
        int(value)
        for value in session.scalars(select(EvalDataset.id).where(EvalDataset.name == DATASET_NAME))
    ]


def _topic_ids(session: Session) -> list[int]:
    """返回种下的主题 id。"""
    rows = session.scalars(select(Topic.id).where(Topic.name == TOPIC_NAME))
    return [int(value) for value in rows]


def _cleanup(session: Session, actor_ids: dict[str, int]) -> None:
    """删掉一轮 T15 冒烟创建的全部行，从叶到根。

    删除顺序是显式写出来的，不交给级联，这样同一串语句在 MySQL 与 SQLite 替身上都
    成立。报告同时以 `RESTRICT` 引用 `topics` 与 `agent_runs`，所以它必须排在两个
    父表之前。
    """
    dataset_ids = _t15_dataset_ids(session)
    if dataset_ids:
        run_ids = select(EvalRun.id).where(EvalRun.dataset_id.in_(dataset_ids))
        session.execute(delete(EvalResult).where(EvalResult.eval_run_id.in_(run_ids)))
        session.execute(delete(EvalRun).where(EvalRun.dataset_id.in_(dataset_ids)))
        session.execute(delete(EvalCase).where(EvalCase.dataset_id.in_(dataset_ids)))
        session.execute(delete(EvalDataset).where(EvalDataset.id.in_(dataset_ids)))

    topic_ids = _topic_ids(session)
    if topic_ids:
        agent_run_ids = select(AgentRun.id).where(AgentRun.topic_id.in_(topic_ids))
        session.execute(delete(Report).where(Report.topic_id.in_(topic_ids)))
        session.execute(
            delete(Report).where(Report.title.startswith(REPORT_TITLE_PREFIX, autoescape=True))
        )
        session.execute(delete(ToolCall).where(ToolCall.agent_run_id.in_(agent_run_ids)))
        session.execute(delete(AgentStep).where(AgentStep.agent_run_id.in_(agent_run_ids)))
        session.execute(delete(AgentRun).where(AgentRun.topic_id.in_(topic_ids)))
        session.execute(delete(PipelineRun).where(PipelineRun.topic_id.in_(topic_ids)))
        session.execute(delete(TopicRepository).where(TopicRepository.topic_id.in_(topic_ids)))
        repository_ids = select(Repository.id).where(
            Repository.provider_repo_id.startswith(PROVIDER_PREFIX, autoescape=True)
        )
        session.execute(
            delete(RepositoryContent).where(RepositoryContent.repository_id.in_(repository_ids))
        )
        session.execute(delete(RepoSnapshot).where(RepoSnapshot.repository_id.in_(repository_ids)))
        session.execute(delete(Topic).where(Topic.id.in_(topic_ids)))

    session.execute(
        delete(Repository).where(
            Repository.provider_repo_id.startswith(PROVIDER_PREFIX, autoescape=True)
        )
    )

    ids = list(actor_ids.values())
    if ids:
        session.execute(delete(UserRole).where(UserRole.user_id.in_(ids)))
        session.execute(delete(User).where(User.id.in_(ids)))
    session.commit()


def _source(session: Session) -> DataSource:
    """返回那个唯一的 GitHub 数据源；首次使用时创建它。"""
    existing = session.scalar(select(DataSource).order_by(DataSource.id).limit(1))
    if existing is not None:
        return existing
    source = DataSource(
        name="github",
        source_type="github",
        base_url="https://github.example.com",
        enabled=True,
        rate_limit_per_minute=30,
    )
    session.add(source)
    session.flush()
    return source


def _pipeline_run(
    session: Session,
    *,
    topic_id: int,
    run_type: str,
    status: str,
    started_at: datetime,
    duration_ms: int,
) -> PipelineRun:
    """写一条带实测耗时的流水线运行。"""
    row = PipelineRun(
        topic_id=topic_id,
        run_type=run_type,
        status=status,
        attempt=1,
        started_at=started_at,
        finished_at=started_at + timedelta(milliseconds=duration_ms),
    )
    session.add(row)
    session.flush()
    return row


def _seed_world(session: Session, actor_ids: dict[str, int], anchor: datetime) -> dict[str, Any]:
    """建出看板要报的主题、仓库、运行、报告与轨迹。"""
    source = _source(session)
    config_id = int(load_or_create_config(session, get_settings()).id)
    topic = Topic(
        name=TOPIC_NAME,
        min_stars=0,
        window_days=7,
        max_candidates=10,
        publish_mode="AUTO_WITH_DELAY",
        enabled=True,
        created_by=actor_ids["admin"],
    )
    session.add(topic)
    session.flush()
    topic_id = int(topic.id)

    repositories: list[int] = []
    for rank, (slug, full_name, stars, forks) in enumerate(
        (
            ("alpha", "t15/alpha", ALPHA_STARS, ALPHA_FORKS),
            ("beta", "t15/beta", BETA_STARS, BETA_FORKS),
        ),
        start=1,
    ):
        repository = Repository(
            source_id=source.id,
            provider_repo_id=f"{PROVIDER_PREFIX}{slug}",
            full_name=full_name,
            html_url=f"https://example.com/{full_name}",
            description=f"T15 smoke repository {full_name}",
            language="Python",
            license="MIT",
            topics_json=["ai", "llm"],
            stars=stars,
            forks=forks,
            first_seen_at=anchor,
            last_seen_at=anchor,
        )
        session.add(repository)
        session.flush()
        repositories.append(int(repository.id))
        session.add(
            TopicRepository(
                topic_id=topic_id,
                repository_id=repository.id,
                score=Decimal("1"),
                rank=rank,
                selected_for_agent=True,
            )
        )
        session.add_all(
            [
                RepoSnapshot(
                    repository_id=repository.id,
                    snapshot_date=(anchor - FIRST_SNAPSHOT).date(),
                    stars=stars - 40,
                    forks=forks - 2,
                ),
                RepoSnapshot(
                    repository_id=repository.id,
                    snapshot_date=(anchor - LAST_SNAPSHOT).date(),
                    stars=stars,
                    forks=forks,
                ),
            ]
        )
        session.add(
            RepositoryContent(
                repository_id=repository.id,
                content_type="readme",
                source_url=f"https://example.com/{full_name}/readme",
                content=f"# {full_name}\nA cached readme for the T15 smoke world.\n",
                content_hash=f"{PROVIDER_PREFIX}{slug}",
                fetched_at=anchor,
            )
        )

    # 一份更早那周发布的报告，好让 `search_prior_reports` 有基线，也让 30/90 天窗口
    # 里有 7 天窗口不该展示的东西。
    prior_pipeline = _pipeline_run(
        session,
        topic_id=topic_id,
        run_type="agent",
        status="succeeded",
        started_at=anchor - PRIOR_REPORT,
        duration_ms=20_000,
    )
    prior_agent = AgentRun(
        pipeline_run_id=prior_pipeline.id,
        topic_id=topic_id,
        agent_config_id=config_id,
        status="completed",
        window_start=anchor - PRIOR_REPORT,
        window_end=anchor - PRIOR_REPORT,
        prompt_tokens=500,
        completion_tokens=100,
        started_at=anchor - PRIOR_REPORT,
        finished_at=anchor - PRIOR_REPORT + timedelta(seconds=20),
    )
    session.add(prior_agent)
    session.flush()
    session.add(
        Report(
            agent_run_id=prior_agent.id,
            topic_id=topic_id,
            owner_id=actor_ids["admin"],
            title=f"{REPORT_TITLE_PREFIX} prior",
            current_version=1,
            content_markdown=f"# {REPORT_TITLE_PREFIX} prior\n",
            status="published",
            visibility="INTERNAL",
            sensitivity="INTERNAL",
            publish_mode="AUTO_WITH_DELAY",
            generated_at=anchor - PRIOR_REPORT,
            published_at=anchor - PRIOR_REPORT,
        )
    )

    # 7 天窗口内的那些行。
    for index, (run_type, status, duration_ms) in enumerate(PIPELINE_ROWS):
        _pipeline_run(
            session,
            topic_id=topic_id,
            run_type=run_type,
            status=status,
            started_at=anchor - RECENT - timedelta(hours=index),
            duration_ms=duration_ms,
        )

    agent_runs: list[int] = []
    for index, row_spec in enumerate(AGENT_ROWS):
        status, mode, error, prompt, completion, cost, duration_ms = row_spec
        started = anchor - RECENT - timedelta(hours=index + 4)
        pipeline = _pipeline_run(
            session,
            topic_id=topic_id,
            run_type="agent",
            status="succeeded" if status != "failed" else "failed",
            started_at=started,
            duration_ms=duration_ms,
        )
        run = AgentRun(
            pipeline_run_id=pipeline.id,
            topic_id=topic_id,
            agent_config_id=config_id,
            status=status,
            window_start=started - timedelta(days=7),
            window_end=started,
            loop_count=4,
            tool_call_count=3,
            model_call_count=5,
            prompt_tokens=prompt,
            completion_tokens=completion,
            estimated_cost=Decimal(cost),
            publish_mode=mode,
            started_at=started,
            finished_at=started + timedelta(milliseconds=duration_ms),
            error_code=error,
        )
        session.add(run)
        session.flush()
        agent_runs.append(int(run.id))

    # 三分之一的报告停在等人处理，这正是 ARCHITECTURE §12 要求的比例。
    for index, (report_status, run_status) in enumerate(
        (("published", "completed"), ("pending_review", "needs_review"))
    ):
        run_id = agent_runs[index]
        generated = anchor - RECENT - timedelta(hours=index + 4)
        session.add(
            Report(
                agent_run_id=run_id,
                topic_id=topic_id,
                owner_id=actor_ids["editor"],
                title=f"{REPORT_TITLE_PREFIX} {run_status}",
                current_version=1,
                content_markdown=f"# {REPORT_TITLE_PREFIX} {run_status}\n",
                status=report_status,
                visibility="TEAM",
                sensitivity="INTERNAL",
                publish_mode="MANUAL" if index == 1 else "AUTO_WITH_DELAY",
                generated_at=generated,
                published_at=generated if report_status == "published" else None,
            )
        )

    # 失败运行的轨迹，其中包含一次没有步骤可挂的工具调用。
    failing = agent_runs[2]
    steps: list[AgentStep] = []
    for index, (step_type, status, summary, latency_ms) in enumerate(
        (
            ("plan", "succeeded", "规划本轮研究问题", 5_000),
            ("tool", "succeeded", "调用 query_repo_metrics 取指标", 300),
            ("tool", "failed", "调用 get_repository_content 被拒绝", 800),
            ("verify", "succeeded", "证据不足，判定为失败收口", 2_000),
        ),
        start=1,
    ):
        step = AgentStep(
            agent_run_id=failing,
            step_index=index,
            step_type=step_type,
            status=status,
            summary=summary,
            latency_ms=latency_ms,
            created_at=anchor - RECENT - timedelta(hours=6, minutes=10 - index),
        )
        session.add(step)
        session.flush()
        steps.append(step)

    session.add_all(
        [
            ToolCall(
                agent_run_id=failing,
                agent_step_id=steps[1].id,
                tool_name="query_repo_metrics",
                arguments_json={"topic_id": topic_id, "field": "stars"},
                result_summary="返回 2 个仓库的指标序列",
                status="success",
                latency_ms=300,
                created_at=anchor - RECENT - timedelta(hours=6, minutes=8),
            ),
            ToolCall(
                agent_run_id=failing,
                agent_step_id=steps[2].id,
                tool_name=DENIED_TOOL,
                arguments_json={"topic_id": topic_id, "repository_id": repositories[0]},
                # 刻意带一个凭据形状的字符串：两条出口路径都必须把它剥掉。
                result_summary=f"读取被拒绝，调试令牌 {LEAKED_SECRET}",
                status="denied",
                error_code=DENIED_ERROR,
                latency_ms=800,
                created_at=anchor - RECENT - timedelta(hours=6, minutes=7),
            ),
            ToolCall(
                agent_run_id=failing,
                # 不挂步骤：外键退化为 null，但这次调用仍必须被报出来。
                agent_step_id=None,
                tool_name="search_prior_reports",
                arguments_json={"topic_id": topic_id},
                result_summary="上游超时",
                status="failed",
                error_code=UNLINKED_TOOL_ERROR,
                latency_ms=200,
                created_at=anchor - RECENT - timedelta(hours=6, minutes=6),
            ),
        ]
    )

    # 一块比看板提供的任何窗口都旧的行，好让「窗口会过滤」成为一条真断言，而不是
    # 一条恰好把一切都选中的查询。
    stale_pipeline = _pipeline_run(
        session,
        topic_id=topic_id,
        run_type="agent",
        status="failed",
        started_at=anchor - STALE,
        duration_ms=5_000,
    )
    stale_agent = AgentRun(
        pipeline_run_id=stale_pipeline.id,
        topic_id=topic_id,
        agent_config_id=config_id,
        status="failed",
        window_start=anchor - STALE,
        window_end=anchor - STALE,
        error_code="AGENT_STALE_TIMEOUT",
        started_at=anchor - STALE,
        finished_at=anchor - STALE + timedelta(seconds=5),
    )
    session.add(stale_agent)
    session.flush()
    session.add(
        Report(
            agent_run_id=stale_agent.id,
            topic_id=topic_id,
            owner_id=actor_ids["admin"],
            title=f"{REPORT_TITLE_PREFIX} stale",
            current_version=1,
            content_markdown=f"# {REPORT_TITLE_PREFIX} stale\n",
            status="failed",
            visibility="TEAM",
            sensitivity="INTERNAL",
            publish_mode="AUTO_WITH_DELAY",
            generated_at=anchor - STALE,
        )
    )

    session.commit()
    return {"topic_id": topic_id, "agent_runs": agent_runs, "failing_run_id": failing}


def _seed_evaluation(session: Session, anchor: datetime, topic_id: int) -> dict[str, Any]:
    """导入一个小数据集，并在它上面真跑一趟评估。

    看板经 `compare_latest` 读最近一次清扫，所以夹具必须是真实运行器产出的清扫。
    手写 `eval_runs` 行，等于拿一个没有任何生产者会产出的形状去测页面。
    """
    start = anchor - FIRST_SNAPSHOT
    end = anchor - timedelta(days=3)
    spec = parse_dataset(
        {
            "schema_version": 1,
            "name": DATASET_NAME,
            "version": DATASET_VERSION,
            "description": "T15 观测样本：一个用例，用于让看板的评测段有真实数据可读。",
            "cases": [
                {
                    "case_key": "observe-happy-path",
                    "topic": TOPIC_NAME,
                    "window": {"start": start.isoformat(), "end": end.isoformat()},
                    "tags": ["t15"],
                    "reference": {
                        "summary": "本期候选仓库整体净增 stars，重点仓库为 t15/alpha。",
                        "required_sections": [
                            "summary",
                            "trend",
                            "highlights",
                            "risks",
                            "recommendation",
                            "appendix",
                        ],
                        "facts": [
                            {"repo": "t15/alpha", "field": "stars", "value": ALPHA_STARS},
                            {"repo": "t15/alpha", "field": "forks", "value": ALPHA_FORKS},
                        ],
                    },
                    "rubric": {
                        "min_citation_coverage": 0.5,
                        "min_factual_consistency": 0.5,
                        "min_task_completion": 0.5,
                    },
                    "enabled": True,
                }
            ],
        }
    )
    imported = import_dataset(session, spec)
    report = run_evaluation(
        session,
        dataset_id=imported.dataset_id,
        target_types=[BASELINE_TARGET, AGENT_TARGET],
        now=anchor,
    )
    return {
        "dataset_id": imported.dataset_id,
        "evaluation_passed": report.passed,
        "evaluation_targets": [item.target_type for item in report.targets],
        "evaluation_case_count": report.case_count,
        "evaluation_target_rows": {
            item.target_type: {
                "status": item.status,
                "scored_case_count": item.scored_case_count,
                "citation_coverage": _as_text(item.metrics.get("citation_coverage")),
                "factual_consistency": _as_text(item.metrics.get("factual_consistency")),
                "task_completion": _as_text(item.metrics.get("task_completion")),
                "gate_failures": list(item.gate_failures),
            }
            for item in report.targets
        },
    }


# --------------------------------------------------------------------------------------
# 判据核对
# --------------------------------------------------------------------------------------


def _inside(value: datetime | None, start: datetime, end: datetime) -> bool:
    """返回时间戳是否落在两侧共用的左闭右开窗口内。"""
    return value is not None and start <= value < end


def _rate(numerator: int, denominator: int) -> str | None:
    """按文档规定的方式算比率：四位小数、四舍五入，未测量时为 `None`。"""
    if denominator <= 0:
        return None
    quantized = (Decimal(numerator) / Decimal(denominator)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    return str(quantized)


def _as_text(value: Decimal | None) -> str | None:
    """把比率渲染成可与 JSON 结果比较的文本，不凭空造一个数。"""
    return None if value is None else str(value)


def _percentile(values: list[int], percent: int) -> int | None:
    """按文档规定的方式算最近秩分位数。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percent / 100 * len(ordered)))
    return ordered[rank - 1]


def _millis(started: datetime | None, finished: datetime | None) -> int | None:
    """返回整毫秒的耗时；行还没结束就返回 `None`。"""
    if started is None or finished is None:
        return None
    return int((finished - started).total_seconds() * 1000)


def _expected_aggregate(session: Session, anchor: datetime, days: int) -> dict[str, Any]:
    """直接从行里重算头条数字，作为第二套实现。

    重点不是把 `collect_metrics` 复述一遍，而是用另一种方式（直接读行，加上文档里的
    公式）推出同一批量，这样聚合层里的缺陷会表现为两边不一致，而不是一个碰巧也对
    的错答案。时钟是传进来的，不在这里读，于是重算与聚合面对的是同一个瞬间。
    """
    start = anchor - timedelta(days=days)
    end = anchor

    pipeline_rows = [
        row for row in session.scalars(select(PipelineRun)) if _inside(row.started_at, start, end)
    ]
    pipeline_outcome = [row for row in pipeline_rows if row.status in PIPELINE_OUTCOME_STATUSES]
    pipeline_statuses = _status_counts(row.status for row in pipeline_rows)
    pipeline_durations = [
        value
        for value in (_millis(row.started_at, row.finished_at) for row in pipeline_rows)
        if value is not None
    ]

    agent_rows = [
        row for row in session.scalars(select(AgentRun)) if _inside(row.started_at, start, end)
    ]
    agent_outcome = [row for row in agent_rows if row.status in AGENT_OUTCOME_STATUSES]
    agent_statuses = _status_counts(row.status for row in agent_rows)
    agent_durations = [
        value
        for value in (_millis(row.started_at, row.finished_at) for row in agent_rows)
        if value is not None
    ]
    prompt_tokens = sum(row.prompt_tokens for row in agent_rows)
    completion_tokens = sum(row.completion_tokens for row in agent_rows)
    total_tokens = prompt_tokens + completion_tokens

    tool_rows = [
        row for row in session.scalars(select(ToolCall)) if _inside(row.created_at, start, end)
    ]
    tool_statuses = _status_counts(row.status for row in tool_rows)

    report_rows = [
        row for row in session.scalars(select(Report)) if _inside(row.generated_at, start, end)
    ]
    report_statuses = _status_counts(row.status for row in report_rows)

    return {
        "pipeline_total": len(pipeline_rows),
        "pipeline_by_status": pipeline_statuses,
        "pipeline_success_rate": _rate(
            sum(1 for row in pipeline_outcome if row.status in PIPELINE_SUCCEEDED_STATUSES),
            len(pipeline_outcome),
        ),
        "pipeline_p50_ms": _percentile(pipeline_durations, 50),
        "pipeline_p95_ms": _percentile(pipeline_durations, 95),
        "agent_total": len(agent_rows),
        "agent_by_status": agent_statuses,
        "agent_prompt_tokens": prompt_tokens,
        "agent_completion_tokens": completion_tokens,
        "agent_total_tokens": total_tokens,
        "agent_cost": str(sum((row.estimated_cost for row in agent_rows), Decimal("0"))),
        "agent_success_rate": _rate(
            sum(1 for row in agent_outcome if row.status in AGENT_SUCCEEDED_STATUSES),
            len(agent_outcome),
        ),
        "agent_failure_reasons": _status_counts(
            row.error_code for row in agent_rows if row.error_code is not None
        ),
        "agent_p50_ms": _percentile(agent_durations, 50),
        "agent_p95_ms": _percentile(agent_durations, 95),
        "tool_total": len(tool_rows),
        "tool_by_status": tool_statuses,
        "tool_success_rate": _rate(
            sum(1 for row in tool_rows if row.status in TOOL_SUCCEEDED_STATUSES), len(tool_rows)
        ),
        "report_total": len(report_rows),
        "report_by_status": report_statuses,
        "report_manual_block_rate": _rate(
            report_statuses.get(MANUAL_BLOCK_STATUS, 0), len(report_rows)
        ),
    }


def _status_counts(values: Iterable[str]) -> dict[str, int]:
    """按状态统计行数并排序，好让两套实现能直接比相等。"""
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _window_checks(session: Session, anchor: datetime, ahead_anchor: datetime) -> dict[str, Any]:
    """判据 1、3、4：数字、窗口，以及「缺席 ≠ 零」这条规则。"""
    metrics = {
        days: collect_metrics(session, now=anchor, window_days=days) for days in (1, 7, 30, 90)
    }
    expected = {days: _expected_aggregate(session, anchor, days) for days in (1, 7, 30, 90)}
    ahead = collect_metrics(session, now=ahead_anchor, window_days=1)
    week = metrics[7]
    day = metrics[1]
    independent = expected[7]
    # 前两条判据声称要展示的每个头条数字，都拿第二套实现核一遍。这里出现 `false`
    # 正是整件事的意义所在。
    agree = {
        f"matches_{name}": actual == independent[name]
        for name, actual in (
            ("pipeline_total", week.pipeline.total),
            ("pipeline_by_status", week.pipeline.by_status),
            ("pipeline_success_rate", _as_text(week.pipeline.success_rate)),
            ("pipeline_p50_ms", week.pipeline.latency.p50_ms),
            ("pipeline_p95_ms", week.pipeline.latency.p95_ms),
            ("agent_total", week.agent.total),
            ("agent_by_status", week.agent.by_status),
            ("agent_prompt_tokens", week.agent.prompt_tokens),
            ("agent_completion_tokens", week.agent.completion_tokens),
            ("agent_total_tokens", week.agent.total_tokens),
            ("agent_cost", str(week.agent.estimated_cost)),
            ("agent_success_rate", _as_text(week.agent.success_rate)),
            ("agent_failure_reasons", week.agent.failure_reasons),
            ("agent_p50_ms", week.agent.latency.p50_ms),
            ("agent_p95_ms", week.agent.latency.p95_ms),
            ("tool_total", week.tools.total),
            ("tool_by_status", week.tools.by_status),
            ("tool_success_rate", _as_text(week.tools.success_rate)),
            ("report_total", week.reports.total),
            ("report_by_status", week.reports.by_status),
            ("report_manual_block_rate", _as_text(week.reports.manual_block_rate)),
        )
    }
    return {
        "window_span_days": (week.window.end - week.window.start) == timedelta(days=7),
        "window_is_half_open": week.window.start < week.window.end,
        "window_clamped": collect_metrics(session, now=anchor, window_days=999).window.days == 90,
        "all_headline_numbers_agree": all(agree.values()),
        **agree,
        **{key: value for key, value in independent.items()},
        # 任务
        "pipeline_by_run_type_7d": week.pipeline.by_run_type,
        # 工具明细
        "tool_names_7d": [item.tool_name for item in week.tools.by_tool],
        "denied_tool_totals_7d": {item.tool_name: item.total for item in week.tools.by_tool},
        # 评测（不受窗口影响）
        "evaluation_available": week.evaluations.available,
        "evaluation_dataset": week.evaluations.dataset_name,
        "evaluation_version": week.evaluations.dataset_version,
        "evaluation_passed": week.evaluations.passed,
        "evaluation_targets": [item.target_type for item in week.evaluations.targets],
        "evaluation_same_in_1d_window": day.evaluations.dataset_id == week.evaluations.dataset_id,
        "evaluation_matches_compare_latest": week.evaluations.passed
        == compare_latest(session, dataset_id=week.evaluations.dataset_id).passed,
        # 窗口把旧数据挡在外面：三个数据块分别落在 7 / 30 / 90 天之内
        "agent_total_7d": metrics[7].agent.total,
        "agent_total_30d": metrics[30].agent.total,
        "agent_total_90d": metrics[90].agent.total,
        "pipeline_total_7d": metrics[7].pipeline.total,
        "pipeline_total_30d": metrics[30].pipeline.total,
        "pipeline_total_90d": metrics[90].pipeline.total,
        "report_total_7d": metrics[7].reports.total,
        "report_total_30d": metrics[30].reports.total,
        "report_total_90d": metrics[90].reports.total,
        "windows_are_strictly_widening": metrics[1].agent.total
        < metrics[7].agent.total
        <= metrics[30].agent.total
        < metrics[90].agent.total,
        "expected_windows_widen": expected[1]["agent_total"]
        < expected[7]["agent_total"]
        <= expected[30]["agent_total"]
        < expected[90]["agent_total"],
        # 未行使 ≠ 0。这一条需要一个真正空的窗口，而任何结束于此刻的窗口都做不到：评测吊具自己
        # 就会在读取时刻写下运行行。唯一保证为空的窗口是尚未发生的那个，于是把探针放在读取时钟
        # 之后两天——它同时也就是新部署上操作者看到的"什么都还没跑"的窗口形状。
        "empty_window_agent_total": ahead.agent.total,
        "empty_window_agent_success_rate": _as_text(ahead.agent.success_rate),
        "empty_window_agent_tokens_per_run": _as_text(ahead.agent.tokens_per_run),
        "empty_window_pipeline_success_rate": _as_text(ahead.pipeline.success_rate),
        "empty_window_tool_success_rate": _as_text(ahead.tools.success_rate),
        "empty_window_report_block_rate": _as_text(ahead.reports.manual_block_rate),
        "empty_window_expected_is_zero": all(
            value == 0
            for key, value in _expected_aggregate(session, ahead_anchor, 1).items()
            if isinstance(value, int)
        ),
        "empty_window_rates_are_null": ahead.agent.success_rate is None
        and ahead.pipeline.success_rate is None
        and ahead.tools.success_rate is None
        and ahead.reports.manual_block_rate is None,
        "empty_window_p50_is_none": ahead.pipeline.latency.p50_ms is None,
        "empty_window_json_keeps_null": ahead.to_dict()["agent"]["success_rate"] is None,
        "empty_window_dash_differs_from_zero": ahead.to_dict()["agent"]["success_rate"] != "0.0000",
    }


def _trace_checks(session: Session, failing_run_id: int) -> dict[str, Any]:
    """判据 5：一次失败的运行能追到那个具体失败的工具调用。"""
    trace = get_agent_run_trace(session, run_id=failing_run_id)
    if trace is None:
        raise RuntimeError("the seeded failing run has no trace")
    denied = next(
        (call for item in trace.steps for call in item.tool_calls if call.status == "denied"),
        None,
    )
    return {
        "trace_step_count": len(trace.steps),
        "trace_step_types": [item.step.step_type for item in trace.steps],
        "trace_step_indexes": [item.step.step_index for item in trace.steps],
        "trace_tool_call_total": sum(len(item.tool_calls) for item in trace.steps),
        "trace_denied_tool": None if denied is None else denied.tool_name,
        "trace_denied_error": None if denied is None else denied.error_code,
        "trace_unlinked_count": len(trace.unlinked_tool_calls),
        "trace_unlinked_tool": (
            trace.unlinked_tool_calls[0].tool_name if trace.unlinked_tool_calls else None
        ),
        "trace_topic_name": trace.topic_name,
        "trace_report_id": trace.report_id,
        "trace_missing_run_is_none": get_agent_run_trace(session, run_id=987_654_321) is None,
    }


def _ledger_checks(session: Session, failing_run_id: int) -> dict[str, Any]:
    """判据 1 之于运行账本：列表按从新到旧、可筛选、可分页。"""
    first_page = list_agent_runs(session, page=1, page_size=5)
    second_page = list_agent_runs(session, page=2, page_size=5)
    by_status = list_agent_runs(session, status="failed", page=1, page_size=50)
    clamped = list_agent_runs(session, page=0, page_size=99_999)
    return {
        "ledger_total": first_page.total,
        "ledger_page_size": first_page.page_size,
        "ledger_first_ids": [int(run.id) for run in first_page.items],
        "ledger_newest_first": [int(run.id) for run in first_page.items]
        == sorted((int(run.id) for run in first_page.items), reverse=True),
        "ledger_pages_disjoint": not (
            {int(run.id) for run in first_page.items} & {int(run.id) for run in second_page.items}
        ),
        "ledger_failed_only": sorted({run.status for run in by_status.items}),
        "ledger_clamped_page": clamped.page,
        "ledger_clamped_page_size": clamped.page_size,
        "ledger_contains_failing_run": failing_run_id in {int(run.id) for run in by_status.items},
    }


def _login(client: TestClient, suffix: str) -> dict[str, str]:
    """经 JSON API 登录一个冒烟用户，并返回它的 Bearer 请求头。"""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": f"{ACTOR_PREFIX}{suffix}__", "password": SMOKE_PASSWORD},
    )
    if response.status_code != 200:
        raise RuntimeError(f"API login for {suffix} failed: {response.status_code} {response.text}")
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _web_login(client: TestClient, suffix: str) -> TestClient:
    """把一个冒烟用户登进浏览器界面，并把会话 Cookie 留在客户端上。"""
    client.get("/ui/login")
    token = client.cookies.get("osia_csrf", "")
    if not token:
        raise RuntimeError("the login page did not hand out a CSRF cookie")
    response = client.post(
        "/ui/login",
        data={
            "username": f"{ACTOR_PREFIX}{suffix}__",
            "password": SMOKE_PASSWORD,
            "next": "/ui/reports",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    if response.status_code != 303:
        raise RuntimeError(f"UI login for {suffix} failed: {response.status_code} {response.text}")
    if "osia_session" not in client.cookies:
        raise RuntimeError(f"UI login for {suffix} set no session cookie")
    return client


def _api_checks(
    client: TestClient,
    admin: dict[str, str],
    failing_run_id: int,
    observed: dict[str, Any],
) -> dict[str, Any]:
    """判据 1-4 与 6 在 HTTP 层，再与直接聚合的结果对账。

    端点与聚合是同一个服务的两个入口，所以每个头条数字回来必须一模一样。这里若不
    一致，意味着传输层在重塑载荷，而不是把它转发出去。
    """
    overview = client.get(METRICS_API, params={"window_days": 7}, headers=admin)
    body = overview.json()
    too_large = client.get(METRICS_API, params={"window_days": 999}, headers=admin)
    too_small = client.get(METRICS_API, params={"window_days": 0}, headers=admin)
    runs = client.get(AGENT_API, headers=admin)
    trace = client.get(f"{AGENT_API}/{failing_run_id}/steps", headers=admin)
    trace_body = trace.json()
    missing = client.get(f"{AGENT_API}/987654321", headers=admin)
    reversed_path = client.get(f"{AGENT_API}/987654321/steps", headers=admin)
    viewer = _login(client, "viewer")
    agree = {
        "api_agrees_pipeline_total": body["pipeline"]["total"] == observed["pipeline_total"],
        "api_agrees_agent_total": body["agent"]["total"] == observed["agent_total"],
        "api_agrees_agent_tokens": body["agent"]["total_tokens"] == observed["agent_total_tokens"],
        "api_agrees_agent_success_rate": body["agent"]["success_rate"]
        == observed["agent_success_rate"],
        "api_agrees_pipeline_success_rate": body["pipeline"]["success_rate"]
        == observed["pipeline_success_rate"],
        "api_agrees_tool_success_rate": body["tools"]["success_rate"]
        == observed["tool_success_rate"],
        "api_agrees_report_block_rate": body["reports"]["manual_block_rate"]
        == observed["report_manual_block_rate"],
        "api_agrees_agent_p95": body["agent"]["latency"]["p95_ms"] == observed["agent_p95_ms"],
    }
    return {
        "api_overview_status": overview.status_code,
        "api_overview_sections": sorted(body),
        "api_overview_window_days": body["window"]["days"],
        "api_overview_pipeline_total": body["pipeline"]["total"],
        "api_overview_agent_total_tokens": body["agent"]["total_tokens"],
        "api_overview_agent_success_rate": body["agent"]["success_rate"],
        "api_overview_report_block_rate": body["reports"]["manual_block_rate"],
        "api_overview_tool_total": body["tools"]["total"],
        "api_overview_tool_success_rate": body["tools"]["success_rate"],
        "api_overview_evaluation_dataset": (body["evaluations"]["dataset"] or {}).get("name"),
        "api_overview_evaluation_passed": body["evaluations"]["passed"],
        **agree,
        "api_all_agree_with_aggregate": all(agree.values()),
        # 越界窗口被拒而不是被夹，与 `page_size` 保持一致。夹取本身在 `_window_checks`
        # 里直接对着聚合层断言。
        "api_too_large_status": too_large.status_code,
        "api_too_small_status": too_small.status_code,
        "api_runs_status": runs.status_code,
        "api_runs_total": runs.json()["total"],
        "api_runs_include_failing": failing_run_id in {item["id"] for item in runs.json()["items"]},
        "api_trace_status": trace.status_code,
        "api_trace_step_count": len(trace_body["steps"]),
        "api_trace_unlinked": len(trace_body["unlinked_tool_calls"]),
        "api_trace_denied_error": next(
            call["error_code"]
            for step in trace_body["steps"]
            for call in step["tool_calls"]
            if call["status"] == "denied"
        ),
        "api_trace_redacts_secret": LEAKED_SECRET not in trace.text,
        "api_trace_keeps_mask": "[REDACTED]" in trace.text,
        "api_missing_run_status": missing.status_code,
        "api_missing_run_code": missing.json().get("code"),
        "api_reversed_path_status": reversed_path.status_code,
        "api_viewer_overview_status": client.get(METRICS_API, headers=viewer).status_code,
        "api_viewer_runs_status": client.get(AGENT_API, headers=viewer).status_code,
        "api_anonymous_status": client.get(METRICS_API).status_code,
    }


def _percent_text(value: str | None) -> str:
    """按模板的方式渲染比率，好让页面里能搜到它。"""
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def _ui_checks(
    client: TestClient,
    failing_run_id: int,
    observed: dict[str, Any],
    denied_error: str,
    ahead_anchor: datetime,
) -> dict[str, Any]:
    """判据 1-7 走渲染后的页面，含角色守卫与导航项。"""
    _web_login(client, "admin")
    week = client.get(DASHBOARD, params={"window_days": 7})
    month = client.get(DASHBOARD, params={"window_days": 30})
    day = client.get(DASHBOARD, params={"window_days": 1})
    # 页面通过依赖注入读时钟，所以把聚合探针用的同一个未来瞬间交给它，就能渲染出空
    # 窗口。否则页面只可能在有数据时被观察到，那条破折号也就永远得不到核对。
    client.app.dependency_overrides[utc_now] = lambda: ahead_anchor
    try:
        empty = client.get(DASHBOARD, params={"window_days": 1})
    finally:
        client.app.dependency_overrides.clear()
    trace = client.get(f"{DASHBOARD}/runs/{failing_run_id}")
    missing = client.get(f"{DASHBOARD}/runs/987654321")
    html = week.text
    text = trace.text
    return {
        "ui_dashboard_status": week.status_code,
        "ui_dashboard_is_html": html.lstrip().startswith("<!DOCTYPE html>"),
        "ui_dashboard_names_window": "7 天" in html,
        "ui_dashboard_shows_pipeline_total": "窗口内任务" in html,
        # 页面里搜的是聚合算出来的数，而不是本脚本里手敲的常量：这样页面必须与服务
        # 一致，而不是与夹具一致。
        "ui_dashboard_shows_tokens": str(observed["agent_total_tokens"]) in html,
        "ui_dashboard_shows_success_rate": _percent_text(observed["agent_success_rate"]) in html,
        "ui_dashboard_shows_block_rate": _percent_text(observed["report_manual_block_rate"])
        in html,
        "ui_dashboard_shows_p50": f"{observed['agent_p50_ms']} ms" in html,
        "ui_dashboard_shows_pipeline_total_value": str(observed["pipeline_total"]) in html,
        "ui_dashboard_shows_evaluation": DATASET_NAME in html,
        "ui_dashboard_window_switcher": html.count("/ui/dashboard?window_days=") >= 3,
        "ui_dashboard_links_runs": f"/ui/dashboard/runs/{failing_run_id}" in html,
        "ui_month_differs_from_week": month.text != html,
        "ui_day_page_status": day.status_code,
        # 空窗口必须渲染成破折号而不是零，这正是「缺席 ≠ 零」规则落到操作者眼里的样子。
        "ui_empty_window_uses_dash": "metric-missing" in empty.text,
        "ui_empty_window_more_dashes": empty.text.count("metric-missing")
        > html.count("metric-missing"),
        "ui_trace_status": trace.status_code,
        "ui_trace_step_count": text.count("trace-step"),
        "ui_trace_names_denied_tool": DENIED_TOOL in text,
        "ui_trace_names_denied_error": denied_error in text,
        "ui_trace_shows_unlinked_section": "未挂到步骤上的工具调用" in text,
        "ui_trace_redacts_secret": LEAKED_SECRET not in text,
        "ui_trace_keeps_mask": "[REDACTED]" in text,
        "ui_missing_run_status": missing.status_code,
        "ui_admin_sees_nav": '<a href="/ui/dashboard">运维看板</a>' in html,
    }


def _ui_guard_checks(client: TestClient) -> dict[str, Any]:
    """判据 6 之于界面：只读角色被拒，且从来看不到导航项。"""
    client.cookies.clear()
    _web_login(client, "viewer")
    dashboard = client.get(DASHBOARD, params={"window_days": 7})
    listing = client.get("/ui/reports")
    return {
        "ui_viewer_dashboard_status": dashboard.status_code,
        "ui_viewer_dashboard_explains": "管理员和编辑者" in dashboard.text,
        "ui_viewer_reports_still_works": listing.status_code,
        "ui_viewer_nav_hides_dashboard": '<a href="/ui/dashboard">' not in listing.text,
    }


def _editor_checks(client: TestClient) -> dict[str, Any]:
    """判据 6：EDITOR 是放行的，可见守卫按角色判定，而不是只认管理员。"""
    client.cookies.clear()
    _web_login(client, "editor")
    dashboard = client.get(DASHBOARD, params={"window_days": 7})
    return {
        "ui_editor_dashboard_status": dashboard.status_code,
        "ui_editor_sees_nav": '<a href="/ui/dashboard">运维看板</a>' in dashboard.text,
    }


def run_checks(session: Session, client: TestClient, failing_run_id: int) -> dict[str, Any]:
    """让真正的聚合、端点与页面走一遍 T15 验收路径。

    读取时钟是在*这里*取的，就在每一行都写完以后。若在建种子之前取，评估吊具自己
    写下的运行就会落到左闭右开上界之外，于是直接聚合会漏掉一行，而几百毫秒之后的
    HTTP 读取却会算上它 —— 两边就会因为一个与被测代码无关的原因对不上。
    """
    settings = get_settings()
    # 刻意*不*截到整秒。被抹掉零头的读取时钟并不严格晚于刚种下的那些行，于是同一秒
    # 里稍后写的行，对进程内聚合来说落在左闭右开上界之外，对片刻之后的 HTTP 读取却
    # 落在界内 —— 两边会因为一个与代码无关的原因不一致。
    anchor = utc_now()
    # 两天而不是一天：`window_days=1` 时窗口是 `[anchor + 1d, anchor + 2d)`，比读取
    # 时钟整整晚一天才开始。若只偏一天，窗口会变成 `[anchor, anchor + 1d)`，把本次
    # 运行自己写下的东西也裹进来。
    ahead_anchor = anchor + timedelta(days=2)
    windows = _window_checks(session, anchor, ahead_anchor)
    _refresh(session)
    trace = _trace_checks(session, failing_run_id)
    _refresh(session)
    ledger = _ledger_checks(session, failing_run_id)
    _refresh(session)
    api = _api_checks(client, _login(client, "admin"), failing_run_id, windows)
    _refresh(session)
    ui = _ui_checks(client, failing_run_id, windows, str(trace["trace_denied_error"]), ahead_anchor)
    _refresh(session)
    guard = _ui_guard_checks(client)
    editor = _editor_checks(client)
    return {
        "agent_provider": settings.agent_provider,
        "eval_enabled": settings.eval_enabled,
        "metrics_roles": ["ADMIN", "EDITOR"],
        "failing_run_id": failing_run_id,
        "read_clock": anchor.isoformat(),
        **windows,
        **trace,
        **ledger,
        **api,
        **ui,
        **guard,
        **editor,
    }


def _parse_args() -> argparse.Namespace:
    """解析验证脚本使用的可选清理模式。"""
    parser = argparse.ArgumentParser(description="Run T15 observability smoke checks")
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Only remove T15 topics, datasets, and records and exit",
    )
    return parser.parse_args()


def main() -> None:
    """在真实数据库上跑 T15 冒烟，并打印断言结果。"""
    args = _parse_args()
    settings: Settings = get_settings()
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)
    # 每一行种下的数据都放在相对这个瞬间的固定偏移上，所以无论哪天跑冒烟，窗口的
    # 含义都一样。它*不是*读取时钟；那个由 `run_checks` 在播种结束后再取。
    seed_clock = utc_now().replace(microsecond=0)

    actor_ids: dict[str, int] = {}
    payload: dict[str, Any] = {}
    try:
        with factory() as session:
            actor_ids = _actor_ids(session)
            _cleanup(session, actor_ids)

        if args.cleanup_only:
            print(json.dumps({"cleanup_only": True, "actors": sorted(actor_ids)}, sort_keys=True))
            return

        with factory() as session:
            actor_ids = _actor_ids(session)
            seeded = _seed_world(session, actor_ids, seed_clock)
            evaluation = _seed_evaluation(session, seed_clock, int(seeded["topic_id"]))
            with TestClient(create_app(settings)) as client:
                payload = run_checks(session, client, int(seeded["failing_run_id"]))
            payload.update(evaluation)
            payload.update(
                {
                    "topic_id": seeded["topic_id"],
                    "seeded_agent_runs": len(seeded["agent_runs"]),
                    "counts": {
                        "pipeline_runs": _count(session, PipelineRun),
                        "agent_runs": _count(session, AgentRun),
                        "agent_steps": _count(session, AgentStep),
                        "tool_calls": _count(session, ToolCall),
                        "reports": _count(session, Report),
                        "eval_datasets": _count(session, EvalDataset),
                        "eval_runs": _count(session, EvalRun),
                    },
                }
            )
    finally:
        with factory() as session:
            _cleanup(session, actor_ids)
        engine.dispose()

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
