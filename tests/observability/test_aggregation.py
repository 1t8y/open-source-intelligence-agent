"""守护可观测性汇总的单元与集成用例。

纯函数直接测，因为它们的定义本身就是关键取舍：nearest-rank 分位数、
四舍五入到四位的比率、以及从未触达的指标取 `None`。
随后再对着灌好的台账测汇总，这样检验的是运维实际看到的结果，
而不只是孤立状态下的行为。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from observability_support import (
    ANCHOR,
    DENIED_TOOL,
    FAILURE_ERROR,
    PRIOR,
    RECENT,
    STALE,
    SeededLedger,
    seed_ledger,
)
from sqlalchemy.orm import Session
from support import Workspace

from open_source_intelligence.observability import metrics
from open_source_intelligence.observability.queries import (
    collect_metrics,
    get_agent_run,
    resolve_window,
)
from open_source_intelligence.persistence.models.intelligence import PipelineRun

# --------------------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------------------


def test_percentile_uses_nearest_rank_not_interpolation() -> None:
    """证明分位数取自某次运行真实花掉的时长，而不是人工插出来的中点。"""
    sample = list(range(1, 101))
    assert metrics.percentile(sample, 50) == 50
    assert metrics.percentile(sample, 95) == 95
    assert metrics.percentile([5], 50) == 5
    assert metrics.percentile([5], 99) == 5
    # 两个样本：P95 取上面那个，而不是两者之间的某个值。
    assert metrics.percentile([10, 20], 95) == 20


def test_percentile_of_an_empty_sample_is_none() -> None:
    """没有被测到的分位数是缺席，不是零。"""
    assert metrics.percentile([], 50) is None
    assert metrics.percentile([], 95) is None


def test_success_rate_of_an_empty_denominator_is_none() -> None:
    """没有任何东西走到结局，就没有成功率，空窗口正是这样读的。"""
    assert metrics.success_rate(succeeded=0, denominator=0) is None
    assert metrics.success_rate(succeeded=0, denominator=-3) is None
    assert metrics.success_rate(succeeded=0, denominator=4) == Decimal("0.0000")
    assert metrics.success_rate(succeeded=3, denominator=4) == Decimal("0.7500")


def test_rates_are_quantized_half_up_to_four_places() -> None:
    """舍入规则沿用评估层那一套，两份报告才不会各说各话。"""
    assert metrics.quantize_rate(Decimal("0.12345")) == Decimal("0.1235")
    assert metrics.success_rate(succeeded=1, denominator=3) == Decimal("0.3333")
    assert metrics.success_rate(succeeded=2, denominator=3) == Decimal("0.6667")


def test_as_rate_keeps_absent_distinct_from_zero() -> None:
    """JSON 边界上仍然保住了汇总所作的区分。"""
    assert metrics.as_rate(None) is None
    assert metrics.as_rate(Decimal("0.0000")) == "0.0000"


def test_count_statuses_omits_unobserved_statuses() -> None:
    """没人走到的状态就不出现，于是载荷展示的是发生过什么，而不是一张网格。"""
    assert metrics.count_statuses(["failed", "failed", "completed"]) == {
        "completed": 1,
        "failed": 2,
    }
    assert metrics.count_statuses([]) == {}


# --------------------------------------------------------------------------------------
# 窗口
# --------------------------------------------------------------------------------------


def test_window_is_half_open_and_measured_in_days() -> None:
    """区间是 `[now - days, now)`，所以相邻两个窗口共享边界，
    却不共享任何一行。
    """
    window = resolve_window(now=ANCHOR, window_days=7)
    assert window.days == 7
    assert window.start == ANCHOR - timedelta(days=7)
    assert window.end == ANCHOR


def test_window_is_clamped_to_the_documented_bounds() -> None:
    """直接调用方不能要到比台账设计读取范围更宽的窗口。"""
    assert resolve_window(now=ANCHOR, window_days=999).days == metrics.MAX_WINDOW_DAYS
    assert resolve_window(now=ANCHOR, window_days=0).days == metrics.MIN_WINDOW_DAYS
    assert resolve_window(now=ANCHOR, window_days=-5).days == metrics.MIN_WINDOW_DAYS


# --------------------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------------------


def _overview(session: Session, *, days: int = 7, offset_days: int = 0):
    """在相对锚点的固定偏移处收集一份概览。"""
    return collect_metrics(
        session,
        now=ANCHOR + timedelta(days=offset_days),
        window_days=days,
    )


def test_aggregation_matches_the_seeded_ledger(session: Session, workspace: Workspace) -> None:
    """每个头部数字都是灌进去那些行所能推出的算术结果。"""
    seed_ledger(session, workspace)
    overview = _overview(session)

    assert overview.pipeline.total == 5
    assert overview.pipeline.by_status == {"failed": 2, "succeeded": 3}
    # 五条都走到了结局，其中三条成功。`queued`/`running` 的行会计进 `total`，
    # 但被排除在这个分母之外；夹具刻意一条都不放，
    # 「等待不算失败」这条规则另由
    # `test_queued_work_is_counted_but_not_judged` 钉住。
    assert overview.pipeline.success_rate == Decimal("0.6000")
    assert overview.pipeline.latency.samples == 5
    # 时长 10s、30s、40s、60s、80s：nearest rank 把 P50 落在第三个样本上。
    assert overview.pipeline.latency.p50_ms == 40_000
    assert overview.pipeline.latency.p95_ms == 80_000

    assert overview.agent.total == 3
    assert overview.agent.by_status == {"completed": 1, "failed": 1, "needs_review": 1}
    assert overview.agent.by_publish_mode == {"AUTO_WITH_DELAY": 1, "MANUAL": 1}
    assert overview.agent.prompt_tokens == 2_500
    assert overview.agent.completion_tokens == 550
    assert overview.agent.total_tokens == 3_050
    assert overview.agent.tokens_per_run == Decimal("1016.6667")
    assert overview.agent.estimated_cost == Decimal("0.020300")
    assert overview.agent.failure_reasons == {FAILURE_ERROR: 1}
    assert overview.agent.latency.p50_ms == 60_000
    assert overview.agent.latency.p95_ms == 80_000

    assert overview.reports.total == 2
    assert overview.reports.by_status == {"pending_review": 1, "published": 1}
    assert overview.reports.manual_block_rate == Decimal("0.5000")


def test_a_parked_run_still_counts_as_a_successful_one(
    session: Session, workspace: Workspace
) -> None:
    """`needs_review` 只是把报告挂起等人看，它产出了报告，不算失败结局。"""
    seed_ledger(session, workspace)
    overview = _overview(session)
    # 窗口内三次运行有两次走到了好结局：completed 与 needs_review。
    assert overview.agent.success_rate == Decimal("0.6667")


def test_a_denied_tool_call_counts_as_a_failure(session: Session, workspace: Workspace) -> None:
    """把拒绝排除在外，会让一个超出工具预算的 Agent 看起来更健康。"""
    seed_ledger(session, workspace)
    overview = _overview(session)
    assert overview.tools.total == 3
    assert overview.tools.by_status == {"denied": 1, "failed": 1, "success": 1}
    assert overview.tools.success_rate == Decimal("0.3333")
    denied = next(item for item in overview.tools.by_tool if item.tool_name == DENIED_TOOL)
    assert denied.by_status == {"denied": 1}
    assert denied.success_rate == Decimal("0.0000")


def test_tools_are_ordered_busiest_first(session: Session, workspace: Workspace) -> None:
    """页面要先把实际用得最多的那个工具排在前面。"""
    seed_ledger(session, workspace)
    overview = _overview(session)
    names = [item.tool_name for item in overview.tools.by_tool]
    assert names == sorted(names)
    assert len(names) == len(set(names))


def test_older_blocks_are_excluded_by_the_window(session: Session, workspace: Workspace) -> None:
    """20 天前的行在 7 天窗口里看不见，在 30 天里看得见，在 1 天里又消失。"""
    seed_ledger(session, workspace)

    assert _overview(session, days=7).agent.total == 3
    assert _overview(session, days=7).reports.total == 2
    assert _overview(session, days=7).pipeline.total == 5

    # 20 天前那一块，只有窗口宽到能碰到它时才会出现。
    assert _overview(session, days=30).agent.total == 4
    assert _overview(session, days=30).pipeline.total == 6
    assert _overview(session, days=30).reports.total == 2

    # 40 天前那一块需要看板提供的最宽窗口。
    assert _overview(session, days=90).agent.total == 5
    assert _overview(session, days=90).pipeline.total == 7
    assert _overview(session, days=90).reports.total == 3

    # 一天的窗口仍然装着最近那一块，这才是「最近」的意思；
    # 掉出去的只有更旧的两块。
    assert _overview(session, days=1).agent.total == 3
    assert _overview(session, days=1).reports.total == 2


def test_an_empty_window_reports_none_rather_than_zero(
    session: Session, workspace: Workspace
) -> None:
    """「还什么都没跑」不能被读成「全都在失败」。

    探针放在读时钟的一天之后。只要灌了真实夹具，
    任何以时钟为终点的窗口都会天然有数据，所以未来窗口是唯一保证为空的窗口 ——
    而它也正是运维在新部署上看到的样子。
    """
    seed_ledger(session, workspace)
    overview = _overview(session, days=1, offset_days=1)

    assert overview.agent.total == 0
    assert overview.pipeline.total == 0
    assert overview.reports.total == 0
    assert overview.tools.total == 0
    assert overview.agent.success_rate is None
    assert overview.agent.tokens_per_run is None
    assert overview.pipeline.success_rate is None
    assert overview.tools.success_rate is None
    assert overview.reports.manual_block_rate is None
    assert overview.pipeline.latency.p50_ms is None
    assert overview.pipeline.latency.samples == 0


def test_queued_work_is_counted_but_not_judged(session: Session, workspace: Workspace) -> None:
    """仍在进行中的运行会出现在总数里，却被排除在成功率之外。

    把「还没跑完」当成「失败」，会让一条繁忙的流水线看起来是坏的；
    而把这一行从总数里丢掉，队列就会消失。两半都在这里断言了。
    """
    seeded = seed_ledger(session, workspace)
    before = _overview(session)
    session.add(
        PipelineRun(
            topic_id=seeded.topic_id,
            run_type="agent",
            status="running",
            attempt=1,
            started_at=ANCHOR - RECENT - timedelta(hours=5),
        )
    )
    session.commit()

    after = _overview(session)
    assert after.pipeline.total == before.pipeline.total + 1
    assert after.pipeline.by_status["running"] == 1
    # 分母没变，所以在飞的运行既不帮忙也不拖累成功率。
    assert after.pipeline.success_rate == before.pipeline.success_rate
    # 未完成的行也不给分位数贡献任何时长。
    assert after.pipeline.latency.samples == before.pipeline.latency.samples
    assert after.pipeline.latency.p50_ms == before.pipeline.latency.p50_ms


def test_the_json_payload_keeps_absent_metrics_null(session: Session, workspace: Workspace) -> None:
    """这个区分扛过了序列化 —— 而那正是最容易凭空造出一个 `0` 的地方。"""
    seed_ledger(session, workspace)
    empty = _overview(session, days=1, offset_days=1).to_dict()
    assert empty["agent"]["success_rate"] is None
    assert empty["agent"]["tokens_per_run"] is None
    assert empty["tools"]["success_rate"] is None
    assert empty["reports"]["manual_block_rate"] is None
    assert empty["pipeline"]["latency"] == {"samples": 0, "p50_ms": None, "p95_ms": None}

    populated = _overview(session).to_dict()
    assert populated["agent"]["success_rate"] == "0.6667"
    assert sorted(populated) == [
        "agent",
        "evaluations",
        "pipeline",
        "reports",
        "tools",
        "window",
    ]


def test_an_empty_evaluation_section_is_marked_unavailable(
    session: Session, workspace: Workspace
) -> None:
    """一次扫描都没跑过，页面就能直说，而不是渲染出一张空表。"""
    seed_ledger(session, workspace)
    evaluation = _overview(session).evaluations
    assert evaluation.available is False
    assert evaluation.dataset_id is None
    assert evaluation.passed is None
    assert evaluation.targets == ()
    assert evaluation.to_dict() == {
        "dataset": None,
        "passed": None,
        "regressions": [],
        "targets": [],
    }


def test_the_window_reports_its_own_bounds(session: Session, workspace: Workspace) -> None:
    """载荷带着它统计所依据的区间，数字因此永远不会脱离语境。"""
    seed_ledger(session, workspace)
    payload = _overview(session).to_dict()
    assert payload["window"]["days"] == 7
    assert payload["window"]["start"] < payload["window"]["end"]
    assert payload["window"]["end"].startswith(ANCHOR.isoformat()[:19])


def test_seeded_ledger_ids_are_reachable(session: Session, workspace: Workspace) -> None:
    """夹具自洽：它交回的 id 指向的正是它写进去的那些行。"""
    seeded: SeededLedger = seed_ledger(session, workspace)
    failing = get_agent_run(session, run_id=seeded.failing_run_id)
    completed = get_agent_run(session, run_id=seeded.completed_run_id)
    assert failing is not None and failing.status == "failed"
    assert completed is not None and completed.status == "completed"
    assert len(seeded.report_generated_ids) == 3
    assert seeded.failing_run_id != seeded.completed_run_id


def test_constants_state_which_outcomes_count() -> None:
    """这些状态集合正是上面各项检查所假定的，这里把它们钉住以防漂移。"""
    assert metrics.PIPELINE_OUTCOME_STATUSES == ("succeeded", "failed", "partial")
    assert metrics.PIPELINE_SUCCEEDED_STATUSES == ("succeeded",)
    assert metrics.AGENT_SUCCEEDED_STATUSES == ("completed", "needs_review")
    assert metrics.AGENT_OUTCOME_STATUSES == ("completed", "needs_review", "failed")
    assert metrics.TOOL_FAILED_STATUSES == ("failed", "denied")
    assert metrics.MANUAL_BLOCK_STATUS == "pending_review"
    assert metrics.DEFAULT_WINDOW_DAYS == 7
    assert PRIOR < STALE
    assert RECENT < PRIOR
