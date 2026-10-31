"""Choosing and dispatching the research runs a daily batch should start.

The daily pipeline ends at `rank` + `contents`. Turning that into a research run was, until this
module, an operator's manual act (`agent/cli.py --action run`), which made "daily report
platform" a claim the code did not back. The gap was never a missing stage - a research run is
not a deterministic stage: it costs real model budget and its output is a draft, not a metric.
What was missing was a rule for *which* topics to spend that budget on, and a guarantee that a
re-delivered task cannot spend it twice.

That is all this module is. It reads the ledger, decides, and hands each topic to a `dispatcher`
the caller supplies - so the worker can queue a Celery task and the CLI can run it inline, using
one definition of "eligible" rather than two.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.agent import AgentRun
from open_source_intelligence.persistence.models.intelligence import PipelineRun, Topic
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.pipeline.contents import business_day_start


@dataclass(frozen=True, slots=True)
class ResearchTarget:
    """One topic that should get a research run today, with the evidence for why.

    `window_start` is where the run's observation window should begin, so a topic that already
    published a report does not re-cover the same period. It is `None` when the topic has no
    published history, in which case the run falls back to the topic's trailing `window_days`.
    """

    topic_id: int
    candidate_count: int
    window_start: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResearchTargetSelection:
    """The (bounded) targets plus what was left behind, so a quiet day reads as one."""

    targets: list[ResearchTarget]
    eligible_count: int
    already_started_topic_ids: list[int]

    @property
    def deferred_count(self) -> int:
        """Eligible topics this batch did not dispatch because the limit was reached."""
        return self.eligible_count - len(self.targets)


@dataclass(frozen=True, slots=True)
class ResearchDispatchReport:
    """Outcome of handing the selection to a dispatcher."""

    dispatched: dict[int, str] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready payload.

        Keys are stringified because this ends up in a Celery result payload, and a JSON object
        cannot carry integer keys - `{14: "id"}` silently becomes `{"14": "id"}` on the wire, so
        the conversion happens here where it can be read rather than discovered later.
        """
        return {
            "dispatched": {str(topic_id): task_id for topic_id, task_id in self.dispatched.items()},
            "errors": {str(topic_id): message for topic_id, message in self.errors.items()},
        }


def _latest_published_window(session: Session, *, topic_id: int) -> datetime | None:
    """Return the window end of the topic's most recent published report, if any.

    The report's own `agent_run` records the window it actually observed (`window_end`), so using
    that as the next run's `window_start` makes consecutive reports abut rather than overlap. A
    topic with no published history returns `None`, which lets `resolve_window` fall back to the
    trailing `window_days` window.
    """
    row = session.scalars(
        select(AgentRun.window_end)
        .join(Report, Report.agent_run_id == AgentRun.id)
        .where(Report.topic_id == topic_id, Report.status == "published")
        .order_by(Report.published_at.desc(), Report.id.desc())
        .limit(1)
    ).first()
    return row


def select_research_targets(
    session: Session,
    *,
    business_date: date,
    timezone_name: str,
    limit: int,
) -> ResearchTargetSelection:
    """Return the topics worth a research run today, bounded to `limit`.

    A topic qualifies when all three hold:

    1. it is enabled - a disabled topic produces no candidates, so there is nothing to research;
    2. today's `rank` run for it succeeded **with candidates** - researching a topic whose ranking
       found nothing spends a model budget to learn that there was nothing to say;
    3. **no research run has been started for it today** - this is the budget guard. A Celery task
       that is re-delivered (worker restart, `acks_late` redelivery, an operator rerunning the
       daily action) must not create a second run and a second charge.

    The `started_at` comparison uses the start of the business day in `timezone_name` converted to
    UTC, the same boundary the contents stage uses. Comparing against midnight UTC would place the
    02:00 scheduled run in the previous day for `Asia/Shanghai` and make every scheduled day look
    like "already researched".

    "Today's rank run" is the newest one for that topic in the window: a rerun of `rank` supersedes
    an earlier failed attempt rather than being blocked by it.
    """
    if limit < 1:
        return ResearchTargetSelection(targets=[], eligible_count=0, already_started_topic_ids=[])

    day_start = business_day_start(business_date, timezone_name)
    # `status != "failed"` rather than "any run": a run that failed for a reason unrelated to the
    # topic (model endpoint down, transport refused) produced nothing to read and nothing to
    # charge twice for, so it must not consume the day's only attempt. Counting it as "already
    # researched" turns one model outage into a silently empty day - exactly what an unattended
    # loop is supposed to survive. `needs_review` and `completed` have a draft behind them, and
    # `pending`/`running` is a run in flight; those are what a second dispatch would duplicate.
    already_started = sorted(
        set(
            session.scalars(
                select(AgentRun.topic_id).where(
                    AgentRun.started_at >= day_start,
                    AgentRun.status != "failed",
                )
            ).all()
        )
    )
    busy = set(already_started)

    ranked = session.execute(
        select(PipelineRun.topic_id, PipelineRun.metrics_json)
        .join(Topic, Topic.id == PipelineRun.topic_id)
        .where(
            PipelineRun.run_type == "rank",
            PipelineRun.status == "succeeded",
            PipelineRun.started_at >= day_start,
            Topic.enabled.is_(True),
        )
        .order_by(PipelineRun.topic_id, PipelineRun.id.desc())
    ).all()

    # One row per topic, newest first: `best` keeps the first sighting and ignores older reruns.
    candidate_counts: dict[int, int] = {}
    for topic_id, metrics in ranked:
        if topic_id is None or topic_id in candidate_counts:
            continue
        candidate_counts[topic_id] = int((metrics or {}).get("candidate_count") or 0)

    eligible = [
        ResearchTarget(
            topic_id=topic_id,
            candidate_count=count,
            window_start=_latest_published_window(session, topic_id=topic_id),
        )
        for topic_id, count in sorted(candidate_counts.items())
        if count > 0 and topic_id not in busy
    ]
    return ResearchTargetSelection(
        targets=eligible[:limit],
        eligible_count=len(eligible),
        already_started_topic_ids=already_started,
    )


def dispatch_research_targets(
    targets: Sequence[ResearchTarget],
    dispatcher: Callable[[ResearchTarget], str],
) -> ResearchDispatchReport:
    """Hand each target to `dispatcher`, surviving one topic's failure.

    The dispatcher receives the full `ResearchTarget` rather than just the topic id, because the
    window the run should observe is part of what is dispatched: two topics can be eligible on
    the same day yet need different `window_start` values, and collapsing that to an id would
    push the window decision back into the caller. The dispatcher returns whatever identifier
    the transport hands back (a Celery task id, a synchronous run marker). A target that raises
    is recorded in `errors` and the rest are still attempted: one topic failing to enqueue must
    not leave the others unresearched, which is the same rule the collector stage follows for one
    unavailable source.
    """
    report = ResearchDispatchReport()
    for target in targets:
        try:
            report.dispatched[target.topic_id] = dispatcher(target)
        except Exception as exc:  # noqa: BLE001 - recorded per topic, never fatal to the batch
            report.errors[target.topic_id] = f"{type(exc).__name__}: {exc}"[:200]
    return report
