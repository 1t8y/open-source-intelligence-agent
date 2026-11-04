"""挑选并派发每日批次应当启动的研究运行。

每日流水线止于 `rank` + `contents`。在有了本模块之前，把它接成一次研究运行是运维的手工
动作（`agent/cli.py --action run`），这让「每日报告平台」成了一个代码并不支撑的说法。缺的
从来不是一个阶段 —— 研究运行并不是确定性阶段：它消耗真实的模型预算，产物是草稿而不是
指标。真正缺的是一条规则，说明这笔预算该花在 *哪些* 主题上，以及一个保证：重复投递的
任务不能把这笔预算花两次。

本模块做的就这些。它读账本、做决定，再把每个主题交给调用方提供的 `dispatcher` —— 于是
Worker 可以排一个 Celery 任务，CLI 可以就地运行，两者共用一套「有资格」的定义，而不是
两套。
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
    """今天应当拿到一次研究运行的主题，以及支撑这一判断的证据。

    `window_start` 是这次运行的观测窗口应当开始的位置，好让已经发布过报告的主题不再覆盖
    同一段时间。主题没有任何已发布历史时它为 `None`，此时运行回退到主题自己的尾随
    `window_days` 窗口。
    """

    topic_id: int
    candidate_count: int
    window_start: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResearchTargetSelection:
    """（有界的）目标，加上被落下的部分，让一个安静的日子读起来就是个安静的日子。"""

    targets: list[ResearchTarget]
    eligible_count: int
    already_started_topic_ids: list[int]

    @property
    def deferred_count(self) -> int:
        """本批因触到上限而没有派发的有资格主题。"""
        return self.eligible_count - len(self.targets)


@dataclass(frozen=True, slots=True)
class ResearchDispatchReport:
    """把选中的目标交给派发器之后的结果。"""

    dispatched: dict[int, str] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """返回可直接转为 JSON 的载荷。

        键要做字符串化，因为这个结果最终进入 Celery 的结果载荷，而 JSON 对象带不了整数
        键 —— `{14: "id"}` 在线上会悄悄变成 `{"14": "id"}`，所以转换放在这里，读代码时
        就能看到，而不是事后才发现。
        """
        return {
            "dispatched": {str(topic_id): task_id for topic_id, task_id in self.dispatched.items()},
            "errors": {str(topic_id): message for topic_id, message in self.errors.items()},
        }


def _latest_published_window(session: Session, *, topic_id: int) -> datetime | None:
    """返回该主题最近一份已发布报告的窗口结束时间，没有则返回 `None`。

    报告自己的 `agent_run` 记录了它实际观测的窗口（`window_end`），把它用作下一次运行的
    `window_start`，能让相邻报告首尾相接而不是互相重叠。没有已发布历史的主题返回 `None`，
    从而让 `resolve_window` 回退到尾随 `window_days` 的窗口。
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
    """返回今天值得跑一次研究的主题，数量上限为 `limit`。

    一个主题要同时满足以下三条才算有资格：

    1. 处于启用状态 —— 停用的主题产不出候选，也就没什么可研究；
    2. 它今天的 `rank` 运行成功、**且带候选** —— 去研究一个排序一无所获的主题，等于花
       模型预算去得知「没什么可说的」；
    3. **今天还没有为它启动过研究运行** —— 这是预算守卫。一个被重复投递的 Celery 任务
       （Worker 重启、`acks_late` 重投、运维重跑每日动作）绝不能创建第二次运行、产生
       第二笔费用。

    `started_at` 的比较用的是 `timezone_name` 下业务日起点换算成 UTC 的时刻，与内容阶段
    用的是同一条边界。拿 UTC 零点比较，会把 02:00 的定时运行算到前一天，让每个定时日都
    显得「已经研究过」。

    「今天的 `rank` 运行」指该主题在窗口内最新的一次：`rank` 重跑会取代更早的失败尝试，
    而不是被它挡住。
    """
    if limit < 1:
        return ResearchTargetSelection(targets=[], eligible_count=0, already_started_topic_ids=[])

    day_start = business_day_start(business_date, timezone_name)
    # 用 `status != "failed"` 而不是「任何运行」：因与主题无关的原因失败的运行（模型端点
    # 挂了、传输被拒）既没产出可读的东西，也没有可重复计费的东西，因此不该消耗掉当天唯一
    # 的一次机会。把它算作「已经研究过」，会让一次模型故障变成静默空白的一天 —— 而这恰恰
    # 是无人值守的循环本该扛过去的情形。`needs_review` 与 `completed` 背后有草稿，
    # `pending`/`running` 是飞行中的运行；这些才是第二次派发会重复出来的东西。
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

    # 每个主题一行，新的在前：保留第一次见到的值，忽略更早的重跑。
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
    """把每个目标交给 `dispatcher`，并让单个主题的失败不影响其余。

    dispatcher 拿到的是完整的 `ResearchTarget`，而不只是主题 id，因为「这次运行该观测哪个
    窗口」本身就是被派发内容的一部分：两个主题可能同一天都有资格，却需要不同的
    `window_start`，把它压成一个 id 等于把窗口决策又推回调用方。dispatcher 返回传输层
    交回的任意标识（一个 Celery 任务 id、一个同步运行的标记）。抛异常的目标记入 `errors`，
    其余目标照常尝试：一个主题入队失败，不能让其它主题因此没被研究，这与采集阶段对单个
    不可用来源遵循的是同一条规则。
    """
    report = ResearchDispatchReport()
    for target in targets:
        try:
            report.dispatched[target.topic_id] = dispatcher(target)
        except Exception as exc:  # noqa: BLE001 - 按主题记录，绝不让整批致命
            report.errors[target.topic_id] = f"{type(exc).__name__}: {exc}"[:200]
    return report
