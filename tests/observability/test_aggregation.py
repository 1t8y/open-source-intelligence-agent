"""Unit and integration tests for the observability aggregation.

The pure functions are tested directly because their definitions are the load-bearing choices:
nearest-rank percentiles, four-place half-up ratios, and `None` for a metric that was never
exercised. The aggregation is then tested against a seeded ledger so the choices are checked as
an operator experiences them rather than only in isolation.
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
# Pure functions
# --------------------------------------------------------------------------------------


def test_percentile_uses_nearest_rank_not_interpolation() -> None:
    """A percentile is a duration some run actually took, never a synthetic midpoint."""
    sample = list(range(1, 101))
    assert metrics.percentile(sample, 50) == 50
    assert metrics.percentile(sample, 95) == 95
    assert metrics.percentile([5], 50) == 5
    assert metrics.percentile([5], 99) == 5
    # Two samples: the upper one is P95 rather than a value between them.
    assert metrics.percentile([10, 20], 95) == 20


def test_percentile_of_an_empty_sample_is_none() -> None:
    """An unmeasured percentile is absent, not zero."""
    assert metrics.percentile([], 50) is None
    assert metrics.percentile([], 95) is None


def test_success_rate_of_an_empty_denominator_is_none() -> None:
    """Nothing reaching an outcome means no rate, which is how a fresh window reads."""
    assert metrics.success_rate(succeeded=0, denominator=0) is None
    assert metrics.success_rate(succeeded=0, denominator=-3) is None
    assert metrics.success_rate(succeeded=0, denominator=4) == Decimal("0.0000")
    assert metrics.success_rate(succeeded=3, denominator=4) == Decimal("0.7500")


def test_rates_are_quantized_half_up_to_four_places() -> None:
    """The rounding rule is the evaluation layer's, so the two reports cannot disagree."""
    assert metrics.quantize_rate(Decimal("0.12345")) == Decimal("0.1235")
    assert metrics.success_rate(succeeded=1, denominator=3) == Decimal("0.3333")
    assert metrics.success_rate(succeeded=2, denominator=3) == Decimal("0.6667")


def test_as_rate_keeps_absent_distinct_from_zero() -> None:
    """The JSON boundary preserves the distinction the aggregation makes."""
    assert metrics.as_rate(None) is None
    assert metrics.as_rate(Decimal("0.0000")) == "0.0000"


def test_count_statuses_omits_unobserved_statuses() -> None:
    """A status nobody reached is absent, so the payload shows what happened rather than a grid."""
    assert metrics.count_statuses(["failed", "failed", "completed"]) == {
        "completed": 1,
        "failed": 2,
    }
    assert metrics.count_statuses([]) == {}


# --------------------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------------------


def test_window_is_half_open_and_measured_in_days() -> None:
    """The span is `[now - days, now)`, so two adjacent windows share an edge but no row."""
    window = resolve_window(now=ANCHOR, window_days=7)
    assert window.days == 7
    assert window.start == ANCHOR - timedelta(days=7)
    assert window.end == ANCHOR


def test_window_is_clamped_to_the_documented_bounds() -> None:
    """A direct caller cannot ask for a window wider than the ledger is meant to be read over."""
    assert resolve_window(now=ANCHOR, window_days=999).days == metrics.MAX_WINDOW_DAYS
    assert resolve_window(now=ANCHOR, window_days=0).days == metrics.MIN_WINDOW_DAYS
    assert resolve_window(now=ANCHOR, window_days=-5).days == metrics.MIN_WINDOW_DAYS


# --------------------------------------------------------------------------------------
# The aggregation
# --------------------------------------------------------------------------------------


def _overview(session: Session, *, days: int = 7, offset_days: int = 0):
    """Collect one overview at a fixed offset from the anchor."""
    return collect_metrics(
        session,
        now=ANCHOR + timedelta(days=offset_days),
        window_days=days,
    )


def test_aggregation_matches_the_seeded_ledger(session: Session, workspace: Workspace) -> None:
    """Every headline number is the arithmetic the seeded rows imply."""
    seed_ledger(session, workspace)
    overview = _overview(session)

    assert overview.pipeline.total == 5
    assert overview.pipeline.by_status == {"failed": 2, "succeeded": 3}
    # All five reached an outcome and three of them succeeded. `queued`/`running` rows would be
    # counted in `total` but kept out of this denominator; the fixture holds none on purpose,
    # so the wait-not-fail rule is pinned in `test_queued_work_is_counted_but_not_judged`.
    assert overview.pipeline.success_rate == Decimal("0.6000")
    assert overview.pipeline.latency.samples == 5
    # Durations 10s, 30s, 40s, 60s, 80s: nearest rank puts P50 on the third sample.
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
    """`needs_review` produced a report and merely parked it, so it is not a losing outcome."""
    seed_ledger(session, workspace)
    overview = _overview(session)
    # Two of the three in-window runs reached a good outcome: completed and needs_review.
    assert overview.agent.success_rate == Decimal("0.6667")


def test_a_denied_tool_call_counts_as_a_failure(session: Session, workspace: Workspace) -> None:
    """Excluding denials would let an Agent that exceeds its tools look healthier."""
    seed_ledger(session, workspace)
    overview = _overview(session)
    assert overview.tools.total == 3
    assert overview.tools.by_status == {"denied": 1, "failed": 1, "success": 1}
    assert overview.tools.success_rate == Decimal("0.3333")
    denied = next(item for item in overview.tools.by_tool if item.tool_name == DENIED_TOOL)
    assert denied.by_status == {"denied": 1}
    assert denied.success_rate == Decimal("0.0000")


def test_tools_are_ordered_busiest_first(session: Session, workspace: Workspace) -> None:
    """The page shows the tool that is actually being used first."""
    seed_ledger(session, workspace)
    overview = _overview(session)
    names = [item.tool_name for item in overview.tools.by_tool]
    assert names == sorted(names)
    assert len(names) == len(set(names))


def test_older_blocks_are_excluded_by_the_window(session: Session, workspace: Workspace) -> None:
    """A 20-day-old row is invisible at 7 days, visible at 30, and absent again at 1."""
    seed_ledger(session, workspace)

    assert _overview(session, days=7).agent.total == 3
    assert _overview(session, days=7).reports.total == 2
    assert _overview(session, days=7).pipeline.total == 5

    # The 20-day-old block appears only once the window is wide enough to reach it.
    assert _overview(session, days=30).agent.total == 4
    assert _overview(session, days=30).pipeline.total == 6
    assert _overview(session, days=30).reports.total == 2

    # The 40-day-old block needs the widest window the dashboard offers.
    assert _overview(session, days=90).agent.total == 5
    assert _overview(session, days=90).pipeline.total == 7
    assert _overview(session, days=90).reports.total == 3

    # A one-day window still holds the recent block, which is what "recent" means; only the two
    # older blocks drop out.
    assert _overview(session, days=1).agent.total == 3
    assert _overview(session, days=1).reports.total == 2


def test_an_empty_window_reports_none_rather_than_zero(
    session: Session, workspace: Workspace
) -> None:
    """'Nothing has run yet' must not read as 'everything is failing'.

    The probe sits one day ahead of the read clock. Any window ending at the clock would be
    populated by construction once a real fixture is seeded, so a future window is the only one
    that is guaranteed empty - and it is the same shape an operator sees on a fresh deployment.
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
    """A run still in flight is visible in the total and kept out of the success rate.

    Treating "has not finished" as "failed" would make a busy pipeline look broken, and dropping
    the row from the total would make a queue invisible. Both halves are asserted here.
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
    # The denominator is unchanged, so an in-flight run neither helps nor hurts the rate.
    assert after.pipeline.success_rate == before.pipeline.success_rate
    # And an unfinished row contributes no duration to the percentiles.
    assert after.pipeline.latency.samples == before.pipeline.latency.samples
    assert after.pipeline.latency.p50_ms == before.pipeline.latency.p50_ms


def test_the_json_payload_keeps_absent_metrics_null(session: Session, workspace: Workspace) -> None:
    """The distinction survives serialization, which is where a `0` would be easy to invent."""
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
    """No sweep has ever run, so the page can say so instead of rendering an empty table."""
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
    """The payload carries the span it was computed over, so a number is never context-free."""
    seed_ledger(session, workspace)
    payload = _overview(session).to_dict()
    assert payload["window"]["days"] == 7
    assert payload["window"]["start"] < payload["window"]["end"]
    assert payload["window"]["end"].startswith(ANCHOR.isoformat()[:19])


def test_seeded_ledger_ids_are_reachable(session: Session, workspace: Workspace) -> None:
    """The fixture is self-consistent: the ids it hands back refer to the rows it wrote."""
    seeded: SeededLedger = seed_ledger(session, workspace)
    failing = get_agent_run(session, run_id=seeded.failing_run_id)
    completed = get_agent_run(session, run_id=seeded.completed_run_id)
    assert failing is not None and failing.status == "failed"
    assert completed is not None and completed.status == "completed"
    assert len(seeded.report_generated_ids) == 3
    assert seeded.failing_run_id != seeded.completed_run_id


def test_constants_state_which_outcomes_count() -> None:
    """The status sets are the ones the checks above assume; this pins them against drift."""
    assert metrics.PIPELINE_OUTCOME_STATUSES == ("succeeded", "failed", "partial")
    assert metrics.PIPELINE_SUCCEEDED_STATUSES == ("succeeded",)
    assert metrics.AGENT_SUCCEEDED_STATUSES == ("completed", "needs_review")
    assert metrics.AGENT_OUTCOME_STATUSES == ("completed", "needs_review", "failed")
    assert metrics.TOOL_FAILED_STATUSES == ("failed", "denied")
    assert metrics.MANUAL_BLOCK_STATUS == "pending_review"
    assert metrics.DEFAULT_WINDOW_DAYS == 7
    assert PRIOR < STALE
    assert RECENT < PRIOR
