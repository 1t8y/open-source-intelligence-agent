"""可观测性测试共用的种子数据辅助。

它放在 `tests/` 顶层，是因为 API 与 Web 两套用例都要用它，
而各个测试目录里并没有 `__init__.py`。

它灌出来的台账刻意做得不大，但也绝不简单：看板提供的每个窗口内都有一块数据，
还有一块比所有这些窗口都旧，结果状态是混合的，另有一次工具调用
的步骤关联已经退化成 null。一个只装着干净成功记录的夹具，
会让失败率与轨迹相关的断言在什么都没测量的情况下通过。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session
from support import Workspace

from open_source_intelligence.persistence.models.agent import AgentRun, AgentStep, ToolCall
from open_source_intelligence.persistence.models.intelligence import PipelineRun
from open_source_intelligence.persistence.models.reporting import Report

#: 所有可观测性用例的对时基准，锚定在*昨天中午*。
#:
#: 它必须跟着时钟走：观测窗口是滚动的（`now - days`），固定锚点
#: 会让台账在跨过日历边界时整体掉出所有窗口。这事真的发生过 ——
#: 灌在 `2026-10-12T12:00Z` 的行，18 号还在七天窗口内，19 号就出了窗口，
#: 于是四个指标 / 看板用例变红，而背后一行代码都没改。
#:
#: 取「一天前」而不是「现在」，是为了让最新的那一段（`ANCHOR - 3h`）
#: 无论套件在几点运行都稳稳落在最窄的窗口里，同时与最新边界保持距离，
#: 这样 `started_at <= window_end` 就不取决于进程是在哪一秒启动的。
ANCHOR = (datetime.now(UTC) - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)

#: 相对锚点的偏移。三段数据，让「窗口会过滤」这句话真的有东西可过滤：
#: 窗口内、7 天窗口外、以及所有看板窗口之外。
RECENT = timedelta(hours=3)
PRIOR = timedelta(days=20)
STALE = timedelta(days=40)

DENIED_TOOL = "get_repository_content"
DENIED_ERROR = "TOOL_NOT_ALLOWED"
FAILURE_ERROR = "AGENT_TOOL_BUDGET_EXCEEDED"
UNLINKED_ERROR = "TOOL_TIMEOUT"

#: 一个凭据形状的值，绝不允许经由任何一条出站路径到达客户端。
LEAKED_SECRET = "sk-Abcdefghij0123456789"


@dataclass(frozen=True, slots=True)
class SeededLedger:
    """可观测性用例用来做断言的各个 id。"""

    topic_id: int
    failing_run_id: int
    completed_run_id: int
    report_generated_ids: tuple[int, ...]


def _agent_run(
    session: Session,
    workspace: Workspace,
    topic_id: int,
    *,
    status: str,
    started_at: datetime,
    duration_ms: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost: str = "0",
    publish_mode: str | None = None,
    error_code: str | None = None,
) -> AgentRun:
    """创建一次 Agent 运行，让它对应的流水线行带上同样的耗时。"""
    run = workspace.add_agent_run(topic_id)
    pipeline = session.get(PipelineRun, run.pipeline_run_id)
    assert pipeline is not None
    pipeline.status = "succeeded" if status != "failed" else "failed"
    pipeline.started_at = started_at
    pipeline.finished_at = started_at + timedelta(milliseconds=duration_ms)
    run.status = status
    run.started_at = started_at
    run.finished_at = started_at + timedelta(milliseconds=duration_ms)
    run.prompt_tokens = prompt_tokens
    run.completion_tokens = completion_tokens
    run.estimated_cost = Decimal(cost)
    run.publish_mode = publish_mode
    run.error_code = error_code
    session.flush()
    return run


def seed_ledger(session: Session, workspace: Workspace) -> SeededLedger:
    """灌入可观测性用例要读的主题、运行、轨迹与报告。"""
    topic = workspace.add_topic()

    # 在每个窗口之内：一次干净运行、一次挂起等人处理、一次失败。
    completed = _agent_run(
        session,
        workspace,
        topic.id,
        status="completed",
        started_at=ANCHOR - RECENT,
        duration_ms=40_000,
        prompt_tokens=1_200,
        completion_tokens=300,
        cost="0.012300",
        publish_mode="AUTO_WITH_DELAY",
    )
    parked = _agent_run(
        session,
        workspace,
        topic.id,
        status="needs_review",
        started_at=ANCHOR - RECENT - timedelta(hours=1),
        duration_ms=60_000,
        prompt_tokens=900,
        completion_tokens=200,
        cost="0.008000",
        publish_mode="MANUAL",
    )
    failing = _agent_run(
        session,
        workspace,
        topic.id,
        status="failed",
        started_at=ANCHOR - RECENT - timedelta(hours=2),
        duration_ms=80_000,
        prompt_tokens=400,
        completion_tokens=50,
        error_code=FAILURE_ERROR,
    )

    # 在 7 天窗口之外，但在 30 天窗口之内。
    _agent_run(
        session,
        workspace,
        topic.id,
        status="completed",
        started_at=ANCHOR - PRIOR,
        duration_ms=20_000,
    )
    # 在所有看板提供的窗口之外。
    _agent_run(
        session,
        workspace,
        topic.id,
        status="failed",
        started_at=ANCHOR - STALE,
        duration_ms=5_000,
        error_code="AGENT_STALE_TIMEOUT",
    )

    # 再加两条别的类型的流水线行，免得运行类型分布只剩一个键。
    for index, (run_type, status, duration_ms) in enumerate(
        (("collect", "succeeded", 10_000), ("rank", "failed", 30_000))
    ):
        session.add(
            PipelineRun(
                topic_id=topic.id,
                run_type=run_type,
                status=status,
                attempt=1,
                started_at=ANCHOR - RECENT - timedelta(hours=index + 3),
                finished_at=ANCHOR
                - RECENT
                - timedelta(hours=index + 3)
                + timedelta(milliseconds=duration_ms),
            )
        )

    # 报告：一份已发布、一份挂起待审、一份比 7 天窗口更旧。
    published = workspace.add_report(
        topic_id=topic.id,
        agent_run_id=completed.id,
        title="T15 published",
        status="published",
        generated_at=ANCHOR - RECENT,
    )
    blocked = workspace.add_report(
        topic_id=topic.id,
        agent_run_id=parked.id,
        title="T15 pending review",
        status="pending_review",
        generated_at=ANCHOR - RECENT - timedelta(hours=1),
    )
    old = workspace.add_report(
        topic_id=topic.id,
        agent_run_id=failing.id,
        title="T15 stale",
        status="failed",
        generated_at=ANCHOR - STALE,
    )

    # 失败运行的轨迹，含一个步骤和一次未关联步骤的工具调用。
    plan = AgentStep(
        agent_run_id=failing.id,
        step_index=1,
        step_type="plan",
        status="succeeded",
        summary="规划本轮研究问题",
        latency_ms=5_000,
        created_at=ANCHOR - RECENT - timedelta(hours=2),
    )
    denied_step = AgentStep(
        agent_run_id=failing.id,
        step_index=2,
        step_type="tool",
        status="failed",
        summary="调用 get_repository_content 被拒绝",
        latency_ms=800,
        created_at=ANCHOR - RECENT - timedelta(hours=2),
    )
    session.add_all([plan, denied_step])
    session.flush()
    session.add_all(
        [
            ToolCall(
                agent_run_id=failing.id,
                agent_step_id=plan.id,
                tool_name="query_repo_metrics",
                arguments_json={"topic_id": topic.id},
                result_summary="返回 1 个仓库的指标序列",
                status="success",
                latency_ms=300,
                created_at=ANCHOR - RECENT - timedelta(hours=2),
            ),
            ToolCall(
                agent_run_id=failing.id,
                agent_step_id=denied_step.id,
                tool_name=DENIED_TOOL,
                arguments_json={"topic_id": topic.id},
                result_summary=f"读取被拒绝，调试令牌 {LEAKED_SECRET}",
                status="denied",
                error_code=DENIED_ERROR,
                latency_ms=800,
                created_at=ANCHOR - RECENT - timedelta(hours=2),
            ),
            ToolCall(
                agent_run_id=failing.id,
                agent_step_id=None,
                tool_name="search_prior_reports",
                arguments_json={"topic_id": topic.id},
                result_summary="上游超时",
                status="failed",
                error_code=UNLINKED_ERROR,
                latency_ms=200,
                created_at=ANCHOR - RECENT - timedelta(hours=2),
            ),
        ]
    )
    session.commit()
    return SeededLedger(
        topic_id=topic.id,
        failing_run_id=failing.id,
        completed_run_id=completed.id,
        report_generated_ids=(published.id, blocked.id, old.id),
    )


def run_count(session: Session) -> int:
    """返回台账当前持有多少次 Agent 运行。"""
    return len(session.scalars(select(AgentRun)).all())


def report_titles(session: Session) -> list[str]:
    """按排序返回所有报告标题，用例据此证明没有写入任何东西。"""
    return sorted(session.scalars(select(Report.title)).all())
