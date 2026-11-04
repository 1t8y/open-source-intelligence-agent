"""守护 `contents` 阶段 —— README 缓存在生产环境里唯一的写者。

这一阶段有两个重点，用例也围绕它们分组：第一，缓存填的恰好是研究运行被允许
读取的那些仓库（选中集合因此不是对授权范围的猜测）；第二，每种结果都单独
记账，事后的「根本没有 README」才不会和「压根没问出来」混为一谈。
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from support import Workspace

from open_source_intelligence.collectors.types import FetchedContent, SourceUnavailableError
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    PipelineRun,
    Repository,
    RepositoryContent,
)
from open_source_intelligence.pipeline.contents import (
    business_day_start,
    run_contents_stage,
    select_content_candidates,
)
from open_source_intelligence.pipeline.runs import PipelineStageError

BUSINESS_DATE = date(2026, 10, 16)


class StubContentSource:
    """按仓库名分别「作答」「答没有」或「拒绝作答」的内容来源替身。"""

    provider = "github"

    def __init__(
        self,
        *,
        missing: tuple[str, ...] = (),
        failing: tuple[str, ...] = (),
        text: str = "# Rocket\n\nIt flies.\n",
    ) -> None:
        self._missing = set(missing)
        self._failing = set(failing)
        self._text = text
        self.calls: list[str] = []

    def fetch_readme(self, full_name: str) -> FetchedContent | None:
        """返回内容；该名称本来就没有时返回 `None`，被拒绝时抛错。"""
        self.calls.append(full_name)
        if full_name in self._failing:
            raise SourceUnavailableError(f"{full_name}: source returned HTTP 500")
        if full_name in self._missing:
            return None
        return FetchedContent(
            provider=self.provider,
            full_name=full_name,
            content_type="readme",
            source_url=f"https://github.com/{full_name}/blob/main/README.md",
            content=self._text,
            fetched_at=datetime.now(UTC),
        )


def data_source(workspace: Workspace, source_type: str = "github") -> DataSource:
    """返回该类型的唯一数据源，首次使用时顺手建出来。

    `data_sources.source_type` 上有唯一约束，而这些用例里的每个主题都由
    同一个来源供给，所以有了这个辅助函数，夹具才不会撞上自己布下的约束。
    """
    existing = workspace.session.scalar(
        select(DataSource).where(DataSource.source_type == source_type)
    )
    if existing is not None:
        return existing
    return workspace.add_source(source_type=source_type)


def seed_topics(
    workspace: Workspace,
    *,
    repositories: int,
    enabled: bool = True,
    selected: bool = True,
    name: str = "AI Agent",
    offset: int = 0,
    source_type: str = "github",
) -> tuple[int, list[int]]:
    """建一个主题并挂 `repositories` 个候选，返回主题 id 与候选 id 列表。"""
    source_id = data_source(workspace, source_type).id
    topic = workspace.add_topic(name=name, enabled=enabled)
    repository_ids: list[int] = []
    for index in range(repositories):
        slug = name.lower().replace(" ", "-")
        repository = workspace.add_repository(
            source_id=source_id,
            provider_repo_id=f"{slug}-{offset + index}",
            full_name=f"acme/{slug}-{offset + index}",
        )
        workspace.link(topic.id, repository.id, rank=index + 1, selected=selected)
        repository_ids.append(repository.id)
    return topic.id, repository_ids


def names_of(session: Session, repository_ids: list[int]) -> list[str]:
    """按 id 顺序返回每个仓库 id 对应的 `full_name`。"""
    rows = session.scalars(
        select(Repository).where(Repository.id.in_(repository_ids)).order_by(Repository.id)
    ).all()
    return [row.full_name for row in rows]


def test_the_selection_is_the_selected_candidates_of_enabled_topics(workspace: Workspace) -> None:
    """停用的主题与未选中的关联行都不该被预热。"""
    _, live_ids = seed_topics(workspace, repositories=2, name="Live")
    seed_topics(workspace, repositories=2, name="Off", enabled=False)
    _, partly_ids = seed_topics(workspace, repositories=3, name="Partly", selected=False)

    candidates = select_content_candidates(workspace.session, limit=50)
    selected_ids = {candidate.repository_id for candidate in candidates}

    assert [candidate.repository_id for candidate in candidates] == live_ids
    assert not set(partly_ids) & selected_ids
    assert all(candidate.provider == "github" for candidate in candidates)


def test_a_repository_selected_by_two_topics_is_asked_about_once(workspace: Workspace) -> None:
    """缓存以仓库为键，所以第二个主题不该再换来一次请求。"""
    source_id = data_source(workspace).id
    first = workspace.add_topic(name="First")
    second = workspace.add_topic(name="Second")
    repository = workspace.add_repository(
        source_id=source_id, provider_repo_id="shared", full_name="acme/shared"
    )
    workspace.link(first.id, repository.id, rank=1)
    workspace.link(second.id, repository.id, rank=1)

    candidates = select_content_candidates(workspace.session, limit=50)

    assert [candidate.repository_id for candidate in candidates] == [repository.id]
    assert candidates[0].topic_id == first.id


def test_ranking_order_and_the_limit_decide_who_is_asked(workspace: Workspace) -> None:
    """上限作用在排名顺序上，最靠前的候选优先。"""
    _, repository_ids = seed_topics(workspace, repositories=4)

    candidates = select_content_candidates(workspace.session, limit=2)

    assert [candidate.repository_id for candidate in candidates] == repository_ids[:2]
    assert [candidate.rank for candidate in candidates] == [1, 2]


def test_every_outcome_is_counted_separately(workspace: Workspace) -> None:
    """已获取、本来没有、来源不支持，是关于三个仓库的三件不同的事。"""
    _, repository_ids = seed_topics(workspace, repositories=3)
    source_names = names_of(workspace.session, repository_ids)
    _, huggingface_ids = seed_topics(
        workspace, repositories=1, name="Models", source_type="huggingface"
    )
    source = StubContentSource(missing=(source_names[1],))

    outcome = run_contents_stage(
        workspace.session,
        business_date=BUSINESS_DATE,
        sources=[source],
        limit=50,
    )

    assert outcome.candidate_count == 4
    assert outcome.summary.fetched == 2
    assert outcome.summary.missing == 1
    assert outcome.summary.unsupported == len(huggingface_ids)
    assert outcome.summary.skipped == 0
    assert outcome.summary.failed == 0
    assert outcome.stage.status == "succeeded"
    assert source.calls == [source_names[0], source_names[1], source_names[2]]


def test_one_refused_repository_does_not_stop_the_others(workspace: Workspace) -> None:
    """来源对某个名称拒绝作答，不该让整批就此结束。"""
    _, repository_ids = seed_topics(workspace, repositories=3)
    repository_names = names_of(workspace.session, repository_ids)
    source = StubContentSource(failing=(repository_names[0],))

    outcome = run_contents_stage(
        workspace.session,
        business_date=BUSINESS_DATE,
        sources=[source],
        limit=50,
    )

    assert outcome.summary.failed == 1
    assert outcome.summary.fetched == 2
    assert outcome.stage.status == "partial"
    assert source.calls == repository_names
    assert len(workspace.session.scalars(select(RepositoryContent)).all()) == 2


def test_all_refused_is_recorded_as_failed_rather_than_partial(workspace: Workspace) -> None:
    """「一条都没拿到」与「拿到了一部分」是两件不同的事。"""
    _, repository_ids = seed_topics(workspace, repositories=2)
    source = StubContentSource(failing=tuple(names_of(workspace.session, repository_ids)))

    outcome = run_contents_stage(
        workspace.session,
        business_date=BUSINESS_DATE,
        sources=[source],
        limit=50,
    )

    run = workspace.session.get(PipelineRun, outcome.stage.run_id)
    assert outcome.stage.status == "failed"
    assert run is not None
    assert run.error_code == "CONTENT_FETCH_FAILED"
    assert run.status == "failed"
    # 全局运行类型不存主题，这正是第二条 CHECK 约束所允许的。
    assert run.topic_id is None


def test_the_run_record_and_the_audit_row_describe_the_pass(workspace: Workspace) -> None:
    """没有容器日志时，账本本身也要能回答「这次刷新做了什么」。"""
    _, repository_ids = seed_topics(workspace, repositories=2)
    source = StubContentSource(missing=(names_of(workspace.session, repository_ids)[1],))
    window = business_day_start(BUSINESS_DATE, "Asia/Shanghai")

    outcome = run_contents_stage(
        workspace.session,
        business_date=BUSINESS_DATE,
        sources=[source],
        limit=7,
        refresh_after=window,
    )

    run = workspace.session.get(PipelineRun, outcome.stage.run_id)
    assert run is not None
    metrics = run.metrics_json or {}
    assert run.run_type == "contents"
    assert metrics["fetched"] == 1
    assert metrics["missing"] == 1
    assert metrics["failed"] == 0
    assert metrics["candidate_count"] == 2
    assert metrics["limit"] == 7
    assert metrics["business_date"] == BUSINESS_DATE.isoformat()
    assert metrics["refresh_after"] == window.isoformat()
    audit = workspace.session.scalar(select(AuditLog).where(AuditLog.action == "content.refresh"))
    assert audit is not None
    assert audit.resource_id == str(run.id)
    assert (audit.metadata_json or {})["fetched"] == 1


def test_a_second_pass_inside_the_window_asks_nothing(workspace: Workspace) -> None:
    """这个窗口就是本阶段的幂等：同一天重跑不能再次拉取。"""
    seed_topics(workspace, repositories=2)
    session = workspace.session
    window = business_day_start(BUSINESS_DATE, "Asia/Shanghai")
    first_source = StubContentSource()
    run_contents_stage(
        session,
        business_date=BUSINESS_DATE,
        sources=[first_source],
        limit=50,
        refresh_after=window,
    )
    assert len(first_source.calls) == 2

    second_source = StubContentSource()
    outcome = run_contents_stage(
        session,
        business_date=BUSINESS_DATE,
        sources=[second_source],
        limit=50,
        refresh_after=window,
    )

    assert second_source.calls == []
    assert outcome.summary.skipped == 2
    assert outcome.summary.fetched == 0
    assert outcome.stage.status == "succeeded"


def test_a_forced_pass_ignores_the_window(workspace: Workspace) -> None:
    """`refresh_after=None` 表示「再问一次」，正是手动刷新想要的。"""
    seed_topics(workspace, repositories=1)
    session = workspace.session
    run_contents_stage(
        session,
        business_date=BUSINESS_DATE,
        sources=[StubContentSource()],
        limit=50,
        refresh_after=business_day_start(BUSINESS_DATE, "Asia/Shanghai"),
    )
    forced_source = StubContentSource()

    outcome = run_contents_stage(
        session,
        business_date=BUSINESS_DATE,
        sources=[forced_source],
        limit=50,
        refresh_after=None,
    )

    assert len(forced_source.calls) == 1
    assert outcome.summary.fetched == 1
    assert outcome.summary.skipped == 0


def test_a_row_confirmed_before_the_window_is_asked_again(workspace: Workspace) -> None:
    """窗口只跳过已经确认过的行，且仅限确认过的行。"""
    seed_topics(workspace, repositories=1)
    session = workspace.session
    window = business_day_start(BUSINESS_DATE, "Asia/Shanghai")
    run_contents_stage(
        session,
        business_date=BUSINESS_DATE,
        sources=[StubContentSource()],
        limit=50,
        refresh_after=window,
    )
    record = session.scalar(select(RepositoryContent))
    assert record is not None
    record.fetched_at = window - timedelta(seconds=1)
    session.commit()

    stale_source = StubContentSource()
    outcome = run_contents_stage(
        session,
        business_date=BUSINESS_DATE,
        sources=[stale_source],
        limit=50,
        refresh_after=window,
    )

    assert len(stale_source.calls) == 1
    assert outcome.summary.fetched == 1
    assert outcome.summary.skipped == 0


def test_no_candidates_is_a_success_with_zero_counts(workspace: Workspace) -> None:
    """平静的一天不是失败，运行记录应该用数字把这件事说清楚。"""
    outcome = run_contents_stage(
        workspace.session,
        business_date=BUSINESS_DATE,
        sources=[StubContentSource()],
        limit=50,
    )

    assert outcome.candidate_count == 0
    assert outcome.stage.status == "succeeded"
    run = workspace.session.get(PipelineRun, outcome.stage.run_id)
    assert run is not None
    assert (run.metrics_json or {})["candidate_count"] == 0


def test_a_missing_source_list_is_refused_rather_than_recorded(workspace: Workspace) -> None:
    """来源列表为空意味着无人能作答，此时写一条成功的运行行就是撒谎。"""
    with pytest.raises(ValueError, match="content source"):
        run_contents_stage(
            workspace.session,
            business_date=BUSINESS_DATE,
            sources=[],
            limit=50,
        )
    assert workspace.session.scalars(select(PipelineRun)).all() == []


def test_a_stage_level_failure_keeps_its_run_record_and_raises(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """哪怕在拉取之前就失败，也必须在账本里看得见。"""
    from open_source_intelligence.pipeline import contents

    def broken(session: Session, *, limit: int) -> list[object]:
        raise RuntimeError("candidate query exploded")

    monkeypatch.setattr(contents, "select_content_candidates", broken)

    with pytest.raises(PipelineStageError) as excinfo:
        run_contents_stage(
            workspace.session,
            business_date=BUSINESS_DATE,
            sources=[StubContentSource()],
            limit=50,
        )

    run = workspace.session.get(PipelineRun, excinfo.value.run_id)
    assert run is not None
    assert run.status == "failed"
    assert run.run_type == "contents"
    assert excinfo.value.error_code == "CONTENT_FETCH_FAILED"


def test_the_cache_window_starts_when_the_business_day_starts() -> None:
    """按应用时区的零点，而不是 UTC 零点 —— 定时运行在 02:00 触发。"""
    window = business_day_start(date(2026, 10, 16), "Asia/Shanghai")

    # 上海的零点在前一天 UTC 16:00，而 02:00 的运行落在窗口之内而不是
    # 之前 —— 正是这一点让同一天的重试不会重新拉取。
    assert window == datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
    assert window < datetime(2026, 10, 16, 2, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
