"""守护可续跑的回填任务，以及推进它们的分页遍历。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from open_source_intelligence import backfill as backfill_package
from open_source_intelligence.backfill import service as backfill_service
from open_source_intelligence.backfill.errors import (
    BackfillJobBusy,
    BackfillLimitError,
    BackfillNotResumable,
    BackfillUnsupportedCollector,
    BackfillWalkFailed,
)
from open_source_intelligence.backfill.jobs import (
    MAX_TARGET_PAGES,
    STALE_AFTER,
    advance_job,
    cancel_job,
    claim_job,
    create_job,
    finish_job,
    list_jobs,
    require_job,
    summarize_job,
)
from open_source_intelligence.backfill.service import resume_backfill, start_backfill
from open_source_intelligence.collectors.limiter import RateLimiter
from open_source_intelligence.collectors.types import (
    CollectedPage,
    CollectedRepository,
    SourceUnavailableError,
)
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.intelligence import PipelineRun, Repository

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
TOTAL_PAGES = 7


class FakeClock:
    """只在有人于其上休眠时才前进的时钟。"""

    def __init__(self) -> None:
        self.now = 500.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class PagedStub:
    """页数固定、且可以指定某一页失败的分页来源。"""

    provider = "github"
    display_name = "GitHub"
    base_url = "https://api.github.com"

    def __init__(self, *, fail_on: int | None = None, total_pages: int = TOTAL_PAGES) -> None:
        self.fail_on = fail_on
        self.total_pages = total_pages
        self.requests: list[int] = []

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        return list(self.collect_page(page=1, per_page=limit).items)

    def collect_page(self, *, page: int, per_page: int) -> CollectedPage:
        self.requests.append(page)
        if page == self.fail_on:
            raise SourceUnavailableError("source returned HTTP 503")
        return CollectedPage(
            page=page,
            items=tuple(
                CollectedRepository(
                    provider=self.provider,
                    provider_repo_id=f"{page}-{index}",
                    full_name=f"org/repo-{page}-{index}",
                    html_url=f"https://github.com/org/repo-{page}-{index}",
                    description=None,
                    language="Python",
                    license="MIT",
                    topics=(),
                    stars=1000 - page,
                    forks=10,
                    observed_at=NOW,
                )
                for index in range(per_page)
            ),
            has_more=page < self.total_pages,
        )

    def describe_query(self) -> dict[str, object]:
        return {"q": "topic:llm stars:>100"}


class OneShotStub:
    """只实现 `collect`、无法分页遍历的采集器。"""

    provider = "github"
    display_name = "GitHub"
    base_url = "https://api.github.com"

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        return []


def build_pacer() -> tuple[FakeClock, RateLimiter]:
    """构造一个跑在假时钟、每两次请求间隔两秒的节流器。"""
    clock = FakeClock()
    return clock, RateLimiter(per_minute=30, clock=clock.time, sleep=clock.sleep)


def repository_count(session: Session) -> int:
    """返回这趟遍历已入库的仓库数。"""
    return session.scalar(select(func.count()).select_from(Repository)) or 0


def backfill_runs(session: Session) -> list[PipelineRun]:
    """按从旧到新返回遍历写下的运行行。"""
    return list(
        session.scalars(
            select(PipelineRun).where(PipelineRun.run_type == "backfill").order_by(PipelineRun.id)
        ).all()
    )


# ------------------------------------------------------------------------ 任务生命周期


def test_a_new_job_starts_before_its_first_page(session: Session) -> None:
    """游标从第 1 页开始，此时还没有任何数据入库。"""
    job = create_job(
        session, provider="github", query={"q": "llm"}, target_pages=4, per_page=10, now=NOW
    )

    assert job.status == "pending"
    assert job.next_page == 1
    assert job.pages_completed == 0
    assert job.items_collected == 0
    assert job.attempt == 1
    assert job.started_at is None


@pytest.mark.parametrize(
    ("target_pages", "per_page"),
    [(0, 10), (MAX_TARGET_PAGES + 1, 10), (5, 0), (5, 101)],
)
def test_a_job_outside_the_bounds_is_refused(
    session: Session, target_pages: int, per_page: int
) -> None:
    """CHECK 约束会拒绝的取值，应用层要先一步拒掉。"""
    with pytest.raises(BackfillLimitError):
        create_job(
            session,
            provider="github",
            query={},
            target_pages=target_pages,
            per_page=per_page,
            now=NOW,
        )


def test_the_first_claim_keeps_attempt_one_and_records_when_it_started(session: Session) -> None:
    """`attempt` 从 1 开始计，与运行账本的惯例一致。"""
    job = create_job(session, provider="github", query={}, now=NOW)

    claim_job(session, job, now=NOW)

    assert job.attempt == 1
    assert job.status == "running"
    assert job.started_at == NOW
    assert job.heartbeat_at == NOW


def test_a_second_claim_counts_as_a_second_attempt(session: Session) -> None:
    """失败后再续跑，是在同一个游标上开始新的一次尝试。"""
    job = create_job(session, provider="github", query={}, now=NOW)
    claim_job(session, job, now=NOW)
    finish_job(session, job, status="failed", error_code="COLLECTOR_FAILED", now=NOW)

    claim_job(session, job, now=NOW + timedelta(minutes=1))

    assert job.attempt == 2
    assert job.error_code is None


def test_claiming_a_job_with_a_live_heartbeat_is_refused(session: Session) -> None:
    """运行中的任务属于正握着它的那趟遍历。"""
    job = create_job(session, provider="github", query={}, now=NOW)
    claim_job(session, job, now=NOW)

    with pytest.raises(BackfillJobBusy) as caught:
        claim_job(session, job, now=NOW + timedelta(minutes=1))

    assert caught.value.job_id == job.id


def test_a_stale_heartbeat_lets_another_walk_take_over(session: Session) -> None:
    """进程已经死掉的遍历，不能把任务永远搁在那里。"""
    job = create_job(session, provider="github", query={}, now=NOW)
    claim_job(session, job, now=NOW)

    claim_job(session, job, now=NOW + STALE_AFTER + timedelta(seconds=1))

    assert job.status == "running"
    assert job.attempt == 2
    assert job.heartbeat_at == NOW + STALE_AFTER + timedelta(seconds=1)


def test_the_heartbeat_tracks_the_last_page_that_committed(session: Session) -> None:
    """活着的遍历每提交一页，就顺手把持有标记续期一次。"""
    job = create_job(session, provider="github", query={}, target_pages=3, now=NOW)
    claim_job(session, job, now=NOW)

    advance_job(session, job, page=1, item_count=2, now=NOW + timedelta(seconds=30))

    assert job.heartbeat_at == NOW + timedelta(seconds=30)
    assert job.next_page == 2


def test_claiming_a_finished_job_is_refused(session: Session) -> None:
    """每一页都落地之后，就没有可遍历的了。"""
    job = create_job(session, provider="github", query={}, target_pages=1, now=NOW)
    claim_job(session, job, now=NOW)
    advance_job(session, job, page=1, item_count=3, now=NOW)
    finish_job(session, job, status="succeeded", now=NOW)

    with pytest.raises(BackfillNotResumable):
        claim_job(session, job, now=NOW + timedelta(hours=1))


def test_advancing_out_of_order_is_refused(session: Session) -> None:
    """游标跳过一页，那一页的数据就会被悄悄丢掉。"""
    job = create_job(session, provider="github", query={}, now=NOW)
    claim_job(session, job, now=NOW)

    with pytest.raises(BackfillNotResumable, match="expected page 1"):
        advance_job(session, job, page=4, item_count=3, now=NOW)


def test_cancelling_keeps_the_cursor_so_the_job_stays_resumable(session: Session) -> None:
    """取消的任务是暂停，而不是作废。"""
    job = create_job(session, provider="github", query={}, target_pages=5, now=NOW)
    claim_job(session, job, now=NOW)
    advance_job(session, job, page=1, item_count=3, now=NOW)

    cancel_job(session, job, reason="operator stopped it", now=NOW)

    assert job.status == "cancelled"
    assert job.next_page == 2
    assert job.pages_completed == 1
    assert summarize_job(job).resumable is True
    claim_job(session, job, now=NOW + timedelta(minutes=1))
    assert job.attempt == 2


def test_the_summary_reports_what_a_resume_command_would_need(session: Session) -> None:
    """`pages_remaining` 与 `resumable` 是运维据此行动的两个事实。"""
    job = create_job(session, provider="github", query={}, target_pages=4, now=NOW)
    claim_job(session, job, now=NOW)
    advance_job(session, job, page=1, item_count=2, now=NOW)
    finish_job(session, job, status="failed", error_code="COLLECTOR_FAILED", now=NOW)

    summary = summarize_job(job).to_dict()

    assert summary["pages_remaining"] == 3
    assert summary["resumable"] is True
    assert summary["error_code"] == "COLLECTOR_FAILED"


def test_the_job_list_filters_by_provider_and_status(session: Session) -> None:
    """运维要找的是失败的那趟遍历，而不是全部遍历。"""
    create_job(session, provider="github", query={}, now=NOW)
    other = create_job(session, provider="huggingface", query={}, now=NOW)
    claim_job(session, other, now=NOW)
    finish_job(session, other, status="failed", error_code="COLLECTOR_FAILED", now=NOW)

    assert len(list_jobs(session, provider="github")) == 1
    assert [job.provider for job in list_jobs(session, status="failed")] == ["huggingface"]
    with pytest.raises(ValueError, match="unsupported job status"):
        list_jobs(session, status="nonsense")


# ------------------------------------------------------------------------- 遍历


def test_a_walk_ingests_every_page_and_closes_the_job(session: Session) -> None:
    """顺利路径：预算用尽、游标停在预算之后，任务成功。"""
    _, limiter = build_pacer()
    stub = PagedStub()

    outcome = start_backfill(
        session, collector=stub, target_pages=5, per_page=4, limiter=limiter, now=NOW
    )

    assert outcome.status == "succeeded"
    assert outcome.pages_this_attempt == 5
    assert outcome.items_this_attempt == 20
    assert outcome.pages_completed == 5
    assert outcome.next_page == 6
    assert outcome.exhausted is False
    assert stub.requests == [1, 2, 3, 4, 5]
    assert repository_count(session) == 20


def test_an_interrupted_walk_resumes_where_it_stopped(session: Session) -> None:
    """最要紧的那条保证：游标能熬过打断遍历的那次失败。

    第一趟遍历在第 3 页阵亡。它已经入库的数据全部留下，游标指在失败的那一页上，
    任务仍然可续跑 —— 于是第二趟从第 3 页开始，而不是第 1 页。
    """
    _, limiter = build_pacer()
    failing = PagedStub(fail_on=3)

    with pytest.raises(BackfillWalkFailed) as caught:
        start_backfill(
            session, collector=failing, target_pages=5, per_page=4, limiter=limiter, now=NOW
        )

    assert caught.value.error_code == "COLLECTOR_FAILED"
    assert caught.value.next_page == 3
    interrupted = require_job(session, caught.value.job_id)
    assert interrupted.status == "failed"
    assert interrupted.pages_completed == 2
    assert interrupted.items_collected == 8
    assert interrupted.next_page == 3
    assert repository_count(session) == 8
    assert failing.requests == [1, 2, 3]

    resumed_stub = PagedStub()
    outcome = resume_backfill(
        session, job_id=interrupted.id, collector=resumed_stub, limiter=limiter, now=NOW
    )

    assert resumed_stub.requests == [3, 4, 5], "the resumed walk re-read pages it had passed"
    assert outcome.started_page == 3
    assert outcome.attempt == 2
    assert outcome.pages_completed == 5
    assert outcome.items_collected == 20
    assert outcome.status == "succeeded"
    assert repository_count(session) == 20


def test_a_walk_records_one_run_row_per_attempt_linked_to_its_job(session: Session) -> None:
    """每次尝试都是独立的一条运行，重试轨迹才留得住。"""
    _, limiter = build_pacer()
    with pytest.raises(BackfillWalkFailed):
        start_backfill(
            session,
            collector=PagedStub(fail_on=2),
            target_pages=3,
            per_page=2,
            limiter=limiter,
            now=NOW,
        )
    resume_backfill(session, job_id=1, collector=PagedStub(), limiter=limiter, now=NOW)

    runs = backfill_runs(session)

    assert [(run.status, run.attempt) for run in runs] == [("failed", 1), ("succeeded", 2)]
    assert [run.error_code for run in runs] == ["COLLECTOR_FAILED", None]
    assert [run.topic_id for run in runs] == [None, None]
    assert runs[1].metrics_json is not None
    assert runs[1].metrics_json["backfill_job_id"] == 1
    assert runs[1].metrics_json["pages_this_attempt"] == 2
    assert runs[1].metrics_json["exhausted"] is False


def test_a_failed_page_is_recorded_on_the_job_and_on_the_run(session: Session) -> None:
    """两份记录彼此一致，读其中一份不必再去看另一份。"""
    _, limiter = build_pacer()

    with pytest.raises(BackfillWalkFailed):
        start_backfill(
            session,
            collector=PagedStub(fail_on=1),
            target_pages=2,
            per_page=2,
            limiter=limiter,
            now=NOW,
        )

    job = require_job(session, 1)
    run = backfill_runs(session)[0]
    assert job.error_code == run.error_code == "COLLECTOR_FAILED"
    assert job.status == run.status == "failed"
    assert "503" in (job.error_message or "")


def test_the_failure_is_audited_as_well_as_recorded(session: Session) -> None:
    """失败的遍历必须留下审计轨迹，而不只是一条运行行。"""
    _, limiter = build_pacer()
    with pytest.raises(BackfillWalkFailed):
        start_backfill(
            session,
            collector=PagedStub(fail_on=1),
            target_pages=2,
            per_page=2,
            limiter=limiter,
            now=NOW,
        )

    event = session.scalars(select(AuditLog).where(AuditLog.action == "backfill.run")).one()

    assert event.result == "failed"
    assert event.resource_type == "backfill_job"
    assert event.resource_id == "1"


def test_a_successful_walk_is_audited_once(session: Session) -> None:
    """成功路径审计的是整趟遍历，而不是每一页。"""
    _, limiter = build_pacer()

    start_backfill(
        session, collector=PagedStub(), target_pages=4, per_page=3, limiter=limiter, now=NOW
    )

    events = session.scalars(select(AuditLog).where(AuditLog.action == "backfill.run")).all()

    assert len(events) == 1
    assert events[0].result == "success"
    assert events[0].metadata_json is not None
    assert events[0].metadata_json["pages_this_attempt"] == 4


def test_a_source_that_runs_out_early_closes_the_job_as_exhausted(session: Session) -> None:
    """预算大于历史长度不算错误，只是历史短一些而已。"""
    _, limiter = build_pacer()

    outcome = start_backfill(
        session, collector=PagedStub(total_pages=3), target_pages=50, per_page=2, limiter=limiter
    )

    assert outcome.exhausted is True
    assert outcome.pages_this_attempt == 3
    assert outcome.next_page == 4
    assert outcome.status == "succeeded"
    assert require_job(session, outcome.job_id).status == "succeeded"


def test_a_single_page_source_stops_after_one_request(session: Session) -> None:
    """首页的 `has_more` 为假，意味着没有第二次请求可花。"""
    _, limiter = build_pacer()
    stub = PagedStub(total_pages=1)

    outcome = start_backfill(
        session, collector=stub, target_pages=10, per_page=3, limiter=limiter, now=NOW
    )

    assert stub.requests == [1]
    assert outcome.exhausted is True
    assert outcome.pages_this_attempt == 1


# ----------------------------------------------------------------------- 节流


def test_a_walk_paces_exactly_one_interval_between_pages(session: Session) -> None:
    """五页只花四个间隔，而且遍历会把它报出来。"""
    clock, limiter = build_pacer()

    outcome = start_backfill(
        session, collector=PagedStub(), target_pages=5, per_page=2, limiter=limiter, now=NOW
    )

    assert clock.slept == [limiter.interval_seconds] * 4
    assert outcome.paced_seconds == pytest.approx(4 * limiter.interval_seconds)


def test_reported_pacing_counts_only_this_walk(session: Session) -> None:
    """跨续跑复用的节流器，不能把第一趟的等待记到第二趟头上。

    结果描述的是单次尝试。若直接读节流器的累计值，续跑那趟就会把被中断那趟
    为了礼让来源而花掉的每一秒都算成自己的。
    """
    _, limiter = build_pacer()
    with pytest.raises(BackfillWalkFailed):
        start_backfill(
            session,
            collector=PagedStub(fail_on=3),
            target_pages=5,
            per_page=2,
            limiter=limiter,
            now=NOW,
        )
    before = limiter.total_waited_seconds

    outcome = resume_backfill(session, job_id=1, collector=PagedStub(), limiter=limiter, now=NOW)

    assert before > 0
    assert outcome.paced_seconds == pytest.approx(limiter.total_waited_seconds - before)
    assert outcome.paced_seconds < limiter.total_waited_seconds


def test_the_walk_defaults_to_the_budget_configured_on_the_source(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """遍历实际执行的，是数据源行里配置的那个速率上限。"""
    captured: list[int] = []
    real = RateLimiter

    def spy(*, per_minute: int) -> RateLimiter:
        captured.append(per_minute)
        clock = FakeClock()
        return real(per_minute=per_minute, clock=clock.time, sleep=clock.sleep)

    monkeypatch.setattr(backfill_service, "RateLimiter", spy)

    start_backfill(session, collector=PagedStub(), target_pages=1, per_page=2, now=NOW)

    assert captured == [30], "the source's own rate_limit_per_minute must be the default"


# ------------------------------------------------------------------ 拒绝与边界


def test_a_collector_that_cannot_page_is_refused(session: Session) -> None:
    """一次性来源无从遍历，明说这一点比勉强翻一页要好。"""
    with pytest.raises(BackfillUnsupportedCollector, match="cannot be walked"):
        start_backfill(session, collector=OneShotStub(), target_pages=2, per_page=2, now=NOW)


def test_walking_the_same_pages_twice_does_not_duplicate_repositories(session: Session) -> None:
    """入库在 (source, provider_repo_id) 上保持幂等，重放一页不必付出代价。

    正因如此，「一页的数据已经写入、游标还没推进」之间的崩溃窗口才无害：即便是
    另一个任务重走同一段历史，也只是更新同样的行，而不是多出一份拷贝。
    """
    _, limiter = build_pacer()
    stub = PagedStub()
    start_backfill(session, collector=stub, target_pages=2, per_page=3, limiter=limiter, now=NOW)
    after_first = repository_count(session)

    second = start_backfill(
        session, collector=stub, target_pages=2, per_page=3, limiter=limiter, now=NOW
    )

    assert after_first == 6
    assert second.items_this_attempt == 6
    assert repository_count(session) == 6


def test_a_walk_that_has_budget_left_but_no_pages_is_refused_up_front(session: Session) -> None:
    """宁可干脆拒绝，也不要让一趟什么都没做的遍历立刻报成功。"""
    job = create_job(session, provider="github", query={}, target_pages=2, now=NOW)
    claim_job(session, job, now=NOW)
    advance_job(session, job, page=1, item_count=1, now=NOW)
    advance_job(session, job, page=2, item_count=1, now=NOW)
    finish_job(session, job, status="failed", error_code="COLLECTOR_FAILED", now=NOW)

    with pytest.raises(BackfillNotResumable, match="no pages left"):
        resume_backfill(session, job_id=job.id, collector=PagedStub(), now=NOW)


def test_the_package_exports_the_public_surface() -> None:
    """模块的 `__all__` 就是其它层赖以导入的契约。"""
    for name in (
        "BackfillJobBusy",
        "BackfillJobNotFound",
        "BackfillLimitError",
        "BackfillNotResumable",
        "BackfillOutcome",
        "BackfillUnsupportedCollector",
        "BackfillWalkFailed",
        "create_job",
        "require_job",
        "resume_backfill",
        "start_backfill",
    ):
        assert hasattr(backfill_package, name)
