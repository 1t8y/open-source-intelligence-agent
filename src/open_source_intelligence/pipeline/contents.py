"""刷新仓库内容缓存，同时也是 `repository_contents` 唯一的生产写入方。

`repository_contents` 让一次研究运行得以自给自足。`get_repository_content` 工具只从这张表
作答、从不访问网络，于是 Agent 能在运行途中读到 README 而不发任何外发请求，报告里对这份
README 的引用也落在一条确实存在的行上。总得有人把表填上；在 T23 之前没人填，因此该工具
总是以 `TOOL_NOT_FOUND` 失败，校验总是得出「没有读到任何缓存的仓库内容」，报告生成则死在
`readme source not found`。

两个位置决策支撑了这套设计：

* **它跑在排序之后，而不是采集内部。** 这个阶段要回答的问题是「哪些仓库值得预热」，而
  排序就是那个答案。为每个已知仓库都抓一次，等于每天各花一次请求，去缓存下一轮永远不会
  读的文档 —— 因为只有被选中的候选才会被研究；放到 GitHub 未认证配额下，这就是宽裕的
  每日批次与半路耗尽之间的差别。
* **这里的失败是一个计数，而不是一个异常。** 某个仓库的 README 抓不到，绝不能终结当天的
  批次，理由与单个 provider 失败时相同：还能做的工作依然值得做。运行记录把 `fetched`、
  `skipped`、`missing` 与 `failed` 分开保存，事后仍能区分「这个仓库没有 README」和
  「我们压根没查出来」。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.collectors.service import upsert_repository_content
from open_source_intelligence.collectors.types import (
    RepositoryContentSource,
    SourceUnavailableError,
)
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    RepositoryContent,
    Topic,
    TopicRepository,
)
from open_source_intelligence.pipeline.runs import (
    ERROR_CONTENT_FAILED,
    PipelineStageError,
    StageOutcome,
    classify_error,
    describe_error,
    fail_pipeline_run,
    finish_pipeline_run,
    mark_pipeline_run_running,
    start_pipeline_run,
)
from open_source_intelligence.security.audit import record_audit_event

#: 本阶段填充的内容类型。与 `repository_contents` 的 CHECK 一致 —— 后者只允许 `readme`；
#: 这个常量存在的意义，是让候选查询、跳过判断与写入都指向同一个名字。
CACHE_CONTENT_TYPE = "readme"

#: 一次抓取失败的消息有多少会进入运行记录。消息本身已由 `describe_error` 脱敏；
#: 这道限长防止二十条失败把一坨指标撑成一份日志文件。
MAX_FAILURE_MESSAGE_CHARS = 200


@dataclass(frozen=True, slots=True)
class ContentCandidate:
    """排序建议预热的一个仓库，以及它的来源。"""

    repository_id: int
    full_name: str
    provider: str
    topic_id: int
    rank: int


@dataclass(slots=True)
class ContentRefreshSummary:
    """单轮刷新按结果分类的计数。"""

    fetched: int = 0
    skipped: int = 0
    missing: int = 0
    failed: int = 0
    unsupported: int = 0
    failures: dict[int, str] = field(default_factory=dict)

    def handled_without_failure(self) -> int:
        """返回有多少候选得到了确定且非失败的结果。"""
        return self.fetched + self.skipped + self.missing + self.unsupported


@dataclass(frozen=True, slots=True)
class ContentsStageOutcome:
    """该阶段的运行记录，外加运维最先查看的那组计数。"""

    stage: StageOutcome
    candidate_count: int
    summary: ContentRefreshSummary


def business_day_start(business_date: date, timezone_name: str) -> datetime:
    """返回给定业务日开始时对应的 UTC 时刻。

    `refresh_after` 会与 `repository_contents.fetched_at` 比较，而后者以 UTC 存储，所以边界
    必须是该业务日在应用时区下的起点再换算成 UTC。用 UTC 零点会落在上海时区的上午，从而让
    02:00 的定时运行落到「昨天」，并在同一天的每次后续运行里重新抓取全部内容。
    """
    timezone = ZoneInfo(timezone_name)
    return datetime.combine(business_date, time.min, tzinfo=timezone).astimezone(UTC)


def select_content_candidates(session: Session, *, limit: int) -> list[ContentCandidate]:
    """返回值得预热的仓库，排名靠前者在前，且不重复。

    选取范围是每个**启用**主题的已选候选，按 `(topic_id, rank)` 排序 —— 这正是研究运行
    被允许读取的那些行，缓存因此覆盖的是授权范围本身，而不是对它的一次猜测。

    被两个主题同时选中的仓库只出现一次，取两个位次中更好的那个：缓存以仓库为键，抓两次
    只是重复同一个请求，而主题之间的先后并不是真实的偏好。
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    rows = session.execute(
        select(
            TopicRepository.repository_id,
            TopicRepository.topic_id,
            TopicRepository.rank,
            Repository.full_name,
            DataSource.source_type,
        )
        .join(Topic, Topic.id == TopicRepository.topic_id)
        .join(Repository, Repository.id == TopicRepository.repository_id)
        .join(DataSource, DataSource.id == Repository.source_id)
        .where(
            Topic.enabled.is_(True),
            TopicRepository.selected_for_agent.is_(True),
        )
        .order_by(TopicRepository.topic_id, TopicRepository.rank)
    ).all()
    candidates: list[ContentCandidate] = []
    seen: set[int] = set()
    for repository_id, topic_id, rank, full_name, provider in rows:
        if repository_id in seen:
            continue
        seen.add(repository_id)
        candidates.append(
            ContentCandidate(
                repository_id=repository_id,
                full_name=full_name,
                provider=provider,
                topic_id=topic_id,
                rank=rank,
            )
        )
        if len(candidates) == limit:
            break
    return candidates


def refresh_repository_contents(
    session: Session,
    *,
    candidates: Sequence[ContentCandidate],
    sources: Mapping[str, RepositoryContentSource],
    refresh_after: datetime | None = None,
) -> ContentRefreshSummary:
    """为每个候选抓取并缓存内容，每处理完一个仓库提交一次。

    逐仓库提交是刻意的：中途失败的抓取不能把已经抓好的仓库一起拖下去，而缓存本来就是一组
    彼此独立、没有任何不变量把它们绑在一起的行。

    `refresh_after` 是幂等窗口。设置后，缓存文本在该时刻或之后确认过的仓库会被直接跳过、
    不发请求；为 `None` 时则逐个询问每个候选，这正是强制刷新想要的效果。
    """
    summary = ContentRefreshSummary()
    for candidate in candidates:
        source = sources.get(candidate.provider)
        if source is None:
            summary.unsupported += 1
            continue
        if refresh_after is not None and _confirmed_since(session, candidate, refresh_after):
            summary.skipped += 1
            continue
        try:
            fetched = source.fetch_readme(candidate.full_name)
        except SourceUnavailableError as exc:
            summary.failed += 1
            summary.failures[candidate.repository_id] = describe_error(exc)[
                :MAX_FAILURE_MESSAGE_CHARS
            ]
            continue
        if fetched is None:
            summary.missing += 1
            continue
        repository = session.get(Repository, candidate.repository_id)
        if repository is None:  # pragma: no cover - 这个 id 由候选查询本身产出
            summary.failed += 1
            summary.failures[candidate.repository_id] = "repository row disappeared"
            continue
        upsert_repository_content(session, repository=repository, fetched=fetched)
        summary.fetched += 1
        session.commit()
    return summary


def _confirmed_since(session: Session, candidate: ContentCandidate, moment: datetime) -> bool:
    """返回该候选的缓存是否在 `moment` 或之后刷新过。

    比较放在 SQL 里而不是 Python 里，好让读到的值与写入时经过同一套类型转换：该列是 UTC，
    而拿不带时区的读回值去比带时区的参数，正是那种会让每个仓库都悄悄显得过期的错配。
    """
    found = session.scalar(
        select(RepositoryContent.id)
        .where(
            RepositoryContent.repository_id == candidate.repository_id,
            RepositoryContent.content_type == CACHE_CONTENT_TYPE,
            RepositoryContent.fetched_at >= moment,
        )
        .limit(1)
    )
    return found is not None


def _stage_status(summary: ContentRefreshSummary, *, candidate_count: int) -> str:
    """把结果计数映射到运行状态词表。"""
    if summary.failed == 0:
        return "succeeded"
    # 没有任何候选得到确定答案：这次运行算失败，而不是部分成功。选中数为零不可能产生失败，
    # 所以能走到这里，就已经说明本来有活要干。
    if summary.handled_without_failure() == 0 and candidate_count > 0:
        return "failed"
    return "partial"


def run_contents_stage(
    session: Session,
    *,
    business_date: date,
    sources: Sequence[RepositoryContentSource],
    limit: int,
    refresh_after: datetime | None = None,
    attempt: int = 1,
) -> ContentsStageOutcome:
    """为排名靠前的候选刷新内容缓存，并记录本次运行。

    Raises:
        PipelineStageError: 阶段本身无法运行时（例如候选查询失败）。单个仓库抓取失败只会
            被计数，且异常会带上为这次失败已经落库的那条记录的 id。
        ValueError: 未提供任何来源时。空的来源列表意味着「没人能回答这个问题」，而记录一次
            什么都没做的运行，等于给一个从未跑过的阶段在账本上记一行绿色；每日编排器
            会直接跳过这个阶段。
    """
    if not sources:
        raise ValueError("at least one content source is required")
    by_provider = {source.provider: source for source in sources}
    run = start_pipeline_run(session, run_type="contents", topic_id=None, attempt=attempt)
    mark_pipeline_run_running(session, run)
    try:
        candidates = select_content_candidates(session, limit=limit)
        summary = refresh_repository_contents(
            session,
            candidates=candidates,
            sources=by_provider,
            refresh_after=refresh_after,
        )
    except Exception as exc:  # noqa: BLE001 - 记录后重新抛出为阶段错误
        error_code = classify_error("contents", exc)
        message = describe_error(exc)
        fail_pipeline_run(session, run, error_code=error_code, error_message=message)
        raise PipelineStageError(
            message,
            run_id=run.id,
            run_type="contents",
            error_code=error_code,
        ) from exc
    status = _stage_status(summary, candidate_count=len(candidates))
    metrics: dict[str, Any] = {
        "business_date": business_date.isoformat(),
        "candidate_count": len(candidates),
        "limit": limit,
        "refresh_after": refresh_after.isoformat() if refresh_after is not None else None,
        "fetched": summary.fetched,
        "skipped": summary.skipped,
        "missing": summary.missing,
        "unsupported": summary.unsupported,
        "failed": summary.failed,
        "failures": {str(key): value for key, value in sorted(summary.failures.items())},
    }
    if status == "failed":
        fail_pipeline_run(
            session,
            run,
            error_code=ERROR_CONTENT_FAILED,
            error_message=f"no repository content could be fetched ({summary.failed} failed)",
            metrics_json=metrics,
        )
    else:
        finish_pipeline_run(session, run, status=status, metrics_json=metrics)
    record_audit_event(
        session,
        action="content.refresh",
        resource_type="pipeline",
        resource_id=str(run.id),
        # 审计词表比运行词表更粗：`partial` 不在它的取值里，而有些仓库抓不到的一轮，
        # 应当归在这条线的严格一侧，而不是宽松一侧。
        result="success" if status == "succeeded" else "failed",
        metadata_json={
            "business_date": metrics["business_date"],
            "candidate_count": len(candidates),
            "fetched": summary.fetched,
            "skipped": summary.skipped,
            "missing": summary.missing,
            "unsupported": summary.unsupported,
            "failed": summary.failed,
        },
    )
    session.commit()
    return ContentsStageOutcome(
        stage=StageOutcome(
            run_id=run.id,
            run_type="contents",
            topic_id=None,
            status=status,
            metrics=metrics,
        ),
        candidate_count=len(candidates),
        summary=summary,
    )
