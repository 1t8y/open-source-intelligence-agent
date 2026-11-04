"""针对 MySQL 跑 T16 的回填与端到端周报冒烟。

T16 的验收标准只有一句话：*完成一次历史回填和一份完整的周报*。本脚本在真实数据库
上、经真实服务把这两件事都做了 —— 除了 socket 的另一端，这里什么都没被 mock ——
并且按标准本身暗示的顺序来做，因为那个顺序本身就是重点：

1. **先走完一段历史。** 真正的 `GitHubCollector` 被指向一个形状与 GitHub 搜索
   API 完全一致的 `httpx.MockTransport`，真正的 `run_backfill` 在上面走。把 mock
   织进*传输层*而不是采集器里，是刻意的：适配器照样拼 URL、照样从 `Link` 头里读
   `has_more`、照样归一化条目，`RetryingHttpClient` 也照样重试。只有答案是人造的。

2. **再做一份周报。** 真正的 `run_daily_pipeline` 在刚才走出来的那些仓库上跑
   collect -> snapshot -> rank，真正的 Agent 产出草稿，真正的组装器把它提升成带
   版本的报告，真正的校验器给它打分，真正的发布状态机经一次真实人工审核把它从
   `draft` 带到 `published`。

这个冒烟要抓的东西，每一条都因为「想当然的实现是错的」：

* **页预算是预算，不是页数。** mock 声称 `total_count=4210`，却拒绝服务第 4 页
  之后，这正是 GitHub 在搜索结果走进 1000 条上限时的行为。用 `total_count` 推算
  末页的遍历者会去请求数据源永远不会回答的页；这一个必须在 `rel="next"` 缺失时
  停下。
* **一次中断只损失在飞的那一页，不损失历史。** 让第 3 页失败，遍历必须记下
  `next_page=3`，续跑必须只请求第 3、4 页，*别的都不要*。若把第 1、2 页再读一遍，
  就等于证明游标只是个装饰。
* **在数据源行上配置的预算，就是实际执行的预算。** 有一趟遍历刻意不传限流器，并
  记录它构造限流器时用的值，于是遍历花掉的数就能与数据源携带的数作对比。
* **崩溃窗口是无害的，不只是「能扛过去」。** 入库对 `(source, provider_repo_id)`
  幂等，所以重放一趟遍历后仓库数分毫不动。检查断的是*计数*，因为那是重复唯一会
  改变的东西。
* **「数据源拒绝」与「我们自己写坏了」保持可区分。** 失败的遍历必须落在
  `COLLECTOR_FAILED` 上，而不是那个兜底的 `PIPELINE_UNEXPECTED_ERROR`。

脚本断言的每一次数据库读取，最后都经一个*全新的*会话完成，所以断言通过不可能是靠
写行那个会话的身份映射解释掉的。全程使用默认的 `agent_provider="offline"` 模型
客户端：它是部署默认值，不需要 key，而且作为对话的纯函数，它让运行保持可复现。
"""

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

import open_source_intelligence.backfill.service as backfill_service
from open_source_intelligence.agent.runtime import DEFAULT_AGENT_CONFIG_NAME, run_research_agent
from open_source_intelligence.backfill.errors import (
    BackfillJobBusy,
    BackfillLimitError,
    BackfillNotResumable,
    BackfillUnsupportedCollector,
    BackfillWalkFailed,
)
from open_source_intelligence.backfill.jobs import (
    FAILED,
    STALE_AFTER,
    SUCCEEDED,
    claim_job,
    create_job,
    list_jobs,
    summarize_job,
)
from open_source_intelligence.backfill.service import (
    resume_backfill,
    run_backfill,
    start_backfill,
)
from open_source_intelligence.collectors.github import GitHubCollector
from open_source_intelligence.collectors.http import RetryingHttpClient
from open_source_intelligence.collectors.limiter import RateLimiter
from open_source_intelligence.collectors.service import ensure_data_source
from open_source_intelligence.collectors.types import CollectedRepository
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.models.agent import (
    AgentConfig,
    AgentRun,
    AgentStep,
    ToolCall,
)
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import (
    BackfillJob,
    PipelineRun,
    Repository,
    RepositoryContent,
    RepoSnapshot,
    Topic,
    TopicRepository,
)
from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportDraft,
    ReportReview,
    ReportSection,
    ReportSource,
    ReportVerification,
    ReportVersion,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.pipeline.daily import run_daily_pipeline
from open_source_intelligence.reports.composition import generate_report
from open_source_intelligence.reports.publishing import (
    publish_due_reports,
    publish_or_request_review,
    record_review,
)
from open_source_intelligence.reports.verification import verify_report

# --------------------------------------------------------------------------------------
# 冒烟自己持有的名字、日期与时钟
# --------------------------------------------------------------------------------------

TOPIC_NAME = "__T16 Weekly Topic__"
KEYWORD = "__t16_weekly__"
SMOKE_QUERY = "__t16_smoke__ stars:>0"
REPO_PREFIX = "__t16_smoke_"
VERIFIER_USERNAME = "__t16_verifier__"

#: 遍历时钟。一个合成瞬间，这样「续跑」一眼就是续跑，而不是一场竞态。
WALK_NOW = datetime(2099, 6, 6, 9, 0, tzinfo=UTC)
#: 周报覆盖的业务日。刻意放到很远的将来：冒烟的快照阶段会碰到*每一个*仓库，所以
#: 它写下的行必须带一个真实运行永远不可能用过的日期，否则冒烟之后的清理会删掉别人
#: 的历史。
BUSINESS_NOW = datetime(2099, 6, 7, 12, 0, tzinfo=UTC)
BUSINESS_DATE = date(2099, 6, 7)
SMOKE_SNAPSHOT_DATES = (BUSINESS_DATE,)

#: 主题交给 Agent 的仓库数。两个，不是十个。确定性的离线客户端会研究它看到的*每一个*
#: 候选 —— 每个一份画像和一条指标序列，外加一次往期报告查询和最多两次 README 复核 ——
#: 也就是 `2n + 3` 次研究工具调用，而预算只有 `agent_max_tool_calls = 10`。按默认的
#: 五个候选算就是十三次：遍历会被中途截断，`budget_degraded` 变真，证据检查也只能
#: 报告自己不完整，于是周报永远过不了校验。两个则留有余量，也正是 T8 研究冒烟已经
#: 端到端证明过的形态。
AGENT_CANDIDATES = 2

#: mock 端点声称持有多少条。每页三条算下来约 1400 页 —— 远超过它实际愿意服务的四页，
#: 这正是重点。
MOCK_TOTAL_COUNT = 4210
MOCK_PAGE_COUNT = 4
MOCK_PER_PAGE = 3
FAILING_PAGE = 3
DAILY_LIMIT = 4

#: 一个间隔，每分钟 60 次请求。下面的步速断言用整间隔而不是秒来写，算式才好读。
PER_MINUTE = 60
INTERVAL_SECONDS = 60.0 / PER_MINUTE

#: 运行期间建的每一个 mock 端点，好让传输层能被确定性地关掉。
_MOCKS: list["MockGitHub"] = []


class FakeClock:
    """一只单调时钟：睡眠是瞬时的，且会被记录下来。

    靠真睡来证明的步速，没人会一直断言下去，所以交给限流器的是时钟与睡眠函数，而不是
    `time`。随后被记录下来的睡眠就是断言本身：相邻两次请求之间，遍历必须恰好花掉一个
    间隔，多一点都不行。
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        """返回当前的假瞬间。"""
        return self.now

    def sleep(self, seconds: float) -> None:
        """记下一次睡眠，并把假瞬间推进同样的秒数。"""
        self.sleeps.append(seconds)
        self.now += seconds

    def jump(self, seconds: float) -> None:
        """推进假瞬间，但不记录成一次睡眠。"""
        self.now += seconds


class MockGitHub:
    """真正的 GitHub 适配器，答案来自一个 GitHub 形状的 mock 传输层。

    mock 只决定*服务端*说了什么，不决定客户端怎么表现。URL 由 `GitHubCollector` 拼，
    `has_more` 由 `parse_link_header` 从真正的 `Link` 头里读出，重试策略属于
    `RetryingHttpClient`，归一化属于适配器。正因如此，这些检查才是在为已发布的代码
    提供证据，而不是在为本文件提供证据。
    """

    def __init__(
        self,
        *,
        label: str,
        page_count: int = MOCK_PAGE_COUNT,
        total_count: int = MOCK_TOTAL_COUNT,
        failing_pages: Sequence[int] = (),
    ) -> None:
        self.label = label
        self.page_count = page_count
        self.total_count = total_count
        self.failing_pages = set(failing_pages)
        self.requests: list[dict[str, Any]] = []
        self._client = httpx.Client(transport=httpx.MockTransport(self._handle), timeout=10)
        self.collector = GitHubCollector(
            RetryingHttpClient(self._client),
            query=SMOKE_QUERY,
        )
        _MOCKS.append(self)

    # -- 服务端一侧 --------------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        """按 GitHub 搜索 API 的方式回答一次搜索请求。"""
        params = dict(request.url.params)
        page = int(params["page"])
        per_page = int(params["per_page"])
        self.requests.append({"page": page, "per_page": per_page, "q": params.get("q", "")})
        if page in self.failing_pages:
            # 一个不带 `Link` 头的 5xx。真正的客户端会按 `max_attempts` 重试若干次才
            # 放弃，所以 `requests_for` 可能数出不止一次命中。
            return httpx.Response(500, json={"message": "server error"})
        items = [self._item(page, index, per_page) for index in range(per_page)]
        # 这个头里只会读 `rel`，所以目标地址只要长得像 URL 就行。
        headers: dict[str, str] = {}
        if page < self.page_count:
            headers["Link"] = (
                f'<https://api.github.com/search/repositories?page={page + 1}>; rel="next", '
                f'<https://api.github.com/search/repositories?page={self.page_count}>; rel="last"'
            )
        return httpx.Response(
            200,
            json={
                "total_count": self.total_count,
                "incomplete_results": False,
                "items": items,
            },
            headers=headers,
        )

    def _item(self, page: int, index: int, per_page: int) -> dict[str, Any]:
        """返回一个 GitHub 仓库对象，编号按它在遍历中的位置来。"""
        number = (page - 1) * per_page + index
        return {
            "id": f"{REPO_PREFIX}{self.label}_{number}",
            "full_name": f"__t16_smoke__/{self.label}-{number}",
            "html_url": f"https://example.com/__t16_smoke__/{self.label}-{number}",
            "description": f"T16 smoke repository for {KEYWORD}",
            "language": "Python",
            "license": {"spdx_id": "MIT"},
            "topics": [KEYWORD],
            "stargazers_count": 500 + number,
            "forks_count": 50 + number,
        }

    # -- 客户端可见的辅助方法 -----------------------------------------------------------

    def open_page(self, page: int) -> None:
        """让某一页不再失败，这正是运维在故障恢复后做的事。"""
        self.failing_pages.discard(page)

    def close(self) -> None:
        """释放这个 mock 持有的传输层。"""
        self._client.close()

    def pages_requested(self) -> list[int]:
        """按顺序返回端点收到的每一次请求的页码。"""
        return [entry["page"] for entry in self.requests]

    def pages_requested_since(self, mark: int) -> list[int]:
        """返回自上一次打标以来请求过的页码。"""
        return [entry["page"] for entry in self.requests[mark:]]

    def requests_for(self, page: int) -> int:
        """返回端点收到过多少次针对某一页的请求。"""
        return sum(1 for entry in self.requests if entry["page"] == page)


class NotPaginated:
    """能采集，但不能被一页一页遍历的采集器。"""

    provider = "t16_not_paginated"
    display_name = "T16 Not Paginated"
    base_url = "https://example.com"

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        """什么也不采集；这个替身存在的唯一意义就是被拒绝。"""
        del limit
        return []


# --------------------------------------------------------------------------------------
# 播种与清理
# --------------------------------------------------------------------------------------


def _smoke_job_ids(session: Session) -> list[int]:
    """返回本冒烟建过的每一个回填任务的 id。

    按任务自己记录的查询来限定，而不是按 provider：已部署的库里有运维的真实任务，
    冒烟的清理没有理由把它们一并扫掉。mock 走的那条查询是本脚本独有的，所以它也是
    现成的最安全的过滤条件。
    """
    return [
        job.id
        for job in session.scalars(select(BackfillJob)).all()
        if (job.query_json or {}).get("q") == SMOKE_QUERY
    ]


def _delete_pipeline_runs(session: Session, runs: Sequence[PipelineRun]) -> int:
    """删掉给定的流水线运行，并返回它们原本有多少条。"""
    if not runs:
        return 0
    session.execute(delete(PipelineRun).where(PipelineRun.id.in_([run.id for run in runs])))
    return len(runs)


def cleanup_verification_records(session: Session) -> dict[str, Any]:
    """删掉上一轮 T16 冒烟创建的全部记录。

    刻意收得很窄。只删本冒烟能证明归自己所有的行：仓库按 provider 前缀匹配，快照按
    一个真实运行绝不可能用过的日期匹配，主题按名字匹配，回填任务按只有本脚本会写的
    那条查询匹配。库里的其他东西 —— 真实采集留下的 22 个仓库、别的冒烟的主题 ——
    原封不动留在原地。
    """
    removed: dict[str, int] = {}

    topic_ids = list(session.scalars(select(Topic.id).where(Topic.name == TOPIC_NAME)).all())
    agent_run_ids: list[int] = []
    if topic_ids:
        run_ids = list(
            session.scalars(select(PipelineRun.id).where(PipelineRun.topic_id.in_(topic_ids))).all()
        )
        if run_ids:
            agent_run_ids = list(
                session.scalars(
                    select(AgentRun.id).where(AgentRun.pipeline_run_id.in_(run_ids))
                ).all()
            )
        agent_run_ids += list(
            session.scalars(select(AgentRun.id).where(AgentRun.topic_id.in_(topic_ids))).all()
        )

    report_ids = (
        list(session.scalars(select(Report.id).where(Report.topic_id.in_(topic_ids))).all())
        if topic_ids
        else []
    )
    if agent_run_ids:
        report_ids += list(
            session.scalars(select(Report.id).where(Report.agent_run_id.in_(agent_run_ids))).all()
        )
    report_ids = sorted(set(report_ids))
    if report_ids:
        session.execute(delete(ReportSource).where(ReportSource.report_id.in_(report_ids)))
        session.execute(delete(ReportReview).where(ReportReview.report_id.in_(report_ids)))
        session.execute(
            delete(ReportVerification).where(ReportVerification.report_id.in_(report_ids))
        )
        session.execute(delete(ReportVersion).where(ReportVersion.report_id.in_(report_ids)))
        session.execute(delete(Report).where(Report.id.in_(report_ids)))
    removed["reports"] = len(report_ids)

    if agent_run_ids:
        draft_ids = select(ReportDraft.id).where(ReportDraft.agent_run_id.in_(agent_run_ids))
        session.execute(delete(ReportSection).where(ReportSection.report_draft_id.in_(draft_ids)))
        session.execute(delete(ReportDraft).where(ReportDraft.agent_run_id.in_(agent_run_ids)))
        session.execute(delete(ToolCall).where(ToolCall.agent_run_id.in_(agent_run_ids)))
        session.execute(delete(AgentStep).where(AgentStep.agent_run_id.in_(agent_run_ids)))
        session.execute(delete(AgentRun).where(AgentRun.id.in_(agent_run_ids)))
    removed["agent_runs"] = len(agent_run_ids)

    job_ids = _smoke_job_ids(session)
    backfill_runs = [
        run
        for run in session.scalars(select(PipelineRun)).all()
        if run.run_type == "backfill"
        or (run.metrics_json or {}).get("backfill_job_id") in job_ids
        or (run.metrics_json or {}).get("business_date") == BUSINESS_DATE.isoformat()
    ]
    if topic_ids:
        backfill_runs += list(
            session.scalars(select(PipelineRun).where(PipelineRun.topic_id.in_(topic_ids))).all()
        )
    removed["pipeline_runs"] = _delete_pipeline_runs(
        session, list({id(run): run for run in backfill_runs}.values())
    )

    if job_ids:
        session.execute(delete(BackfillJob).where(BackfillJob.id.in_(job_ids)))
        # 限定在本冒烟自己的任务上。审计表按设计是只追加的，而正是这条性质决定了此处
        # 的删除必须这么窄：这里删掉的行，是本冒烟在同一口气里正在移除的那些任务自己
        # 写下的。
        session.execute(
            delete(AuditLog).where(
                AuditLog.action == "backfill.run",
                AuditLog.resource_id.in_([str(job_id) for job_id in job_ids]),
            )
        )
    removed["backfill_jobs"] = len(job_ids)

    if topic_ids:
        session.execute(delete(TopicRepository).where(TopicRepository.topic_id.in_(topic_ids)))
        session.execute(delete(Topic).where(Topic.id.in_(topic_ids)))
    removed["topics"] = len(topic_ids)

    snapshots = session.execute(
        delete(RepoSnapshot).where(RepoSnapshot.snapshot_date.in_(SMOKE_SNAPSHOT_DATES))
    )
    removed["repo_snapshots"] = int(snapshots.rowcount or 0)

    repositories = session.scalars(
        select(Repository.id).where(
            Repository.provider_repo_id.startswith(REPO_PREFIX, autoescape=True)
        )
    ).all()
    if repositories:
        session.execute(
            delete(RepositoryContent).where(RepositoryContent.repository_id.in_(repositories))
        )
        session.execute(delete(RepoSnapshot).where(RepoSnapshot.repository_id.in_(repositories)))
        session.execute(delete(Repository).where(Repository.id.in_(repositories)))
    removed["repositories"] = len(repositories)

    # 这份配置由运行时在首次使用时创建；在这里，冒烟是它唯一的消费者。
    session.execute(delete(AgentConfig).where(AgentConfig.name == DEFAULT_AGENT_CONFIG_NAME))
    session.execute(delete(User).where(User.username == VERIFIER_USERNAME))
    session.commit()
    return removed


def seed_topic(session: Session) -> dict[str, int]:
    """建出可问责用户，以及周报所属的那个主题。"""
    user = User(
        username=VERIFIER_USERNAME,
        password_hash="t16-verification-only",
        display_name="T16 Verifier",
        status="active",
        data_scope="team",
    )
    session.add(user)
    session.flush()
    topic = Topic(
        name=TOPIC_NAME,
        description="T16 end-to-end weekly report topic",
        keywords_json=[KEYWORD],
        min_stars=0,
        window_days=7,
        max_candidates=AGENT_CANDIDATES,
        publish_mode="AUTO_WITH_DELAY",
        enabled=True,
        created_by=user.id,
    )
    session.add(topic)
    session.commit()
    return {"user_id": user.id, "topic_id": topic.id}


# --------------------------------------------------------------------------------------
# 第一部分：历史回填
# --------------------------------------------------------------------------------------


def _runs_for_job(session: Session, job_id: int) -> list[PipelineRun]:
    """返回某个任务名下产生的回填运行，从旧到新。

    在 Python 里过滤，而不用 JSON 谓词：`metrics_json` 是 `JSON` 列，只有 MySQL 认得的
    路径查询会让冒烟没法对着镜像它的 SQLite 夹具跑。这里的行数只有区区几条。
    """
    return [
        run
        for run in session.scalars(
            select(PipelineRun).where(PipelineRun.run_type == "backfill").order_by(PipelineRun.id)
        ).all()
        if (run.metrics_json or {}).get("backfill_job_id") == job_id
    ]


def _repo_count(session: Session, prefix: str) -> int:
    """返回带某个标签的那趟遍历落下了多少仓库。"""
    return int(
        session.scalar(
            select(func.count())
            .select_from(Repository)
            .where(
                Repository.provider_repo_id.startswith(f"{REPO_PREFIX}{prefix}_", autoescape=True)
            )
        )
        or 0
    )


def _backfill_checks(session: Session) -> dict[str, Any]:
    """从四个角度走一段历史：走到源头尽头、走进失败、走出失败，再走一遍。"""
    jobs_before = int(session.scalar(select(func.count()).select_from(BackfillJob)) or 0)

    # -- 不支持的采集器在写入任何东西之前就被拒 -----------------------------------------
    unsupported = ""
    try:
        start_backfill(session, collector=NotPaginated(), target_pages=1, per_page=1)
    except BackfillUnsupportedCollector as exc:
        unsupported = str(exc)
    else:
        raise RuntimeError("a collector that cannot page was accepted for a walk")

    # -- 越界参数当场被拒，而不是先存下来、以后再发现 ------------------------------------
    bounds: list[str] = []
    for target_pages, per_page in ((0, 10), (101, 10), (2, 0), (2, 101)):
        try:
            create_job(
                session,
                provider="github",
                query={"q": SMOKE_QUERY},
                target_pages=target_pages,
                per_page=per_page,
            )
        except BackfillLimitError as exc:
            bounds.append(str(exc))
        else:
            raise RuntimeError(f"target_pages={target_pages} per_page={per_page} was accepted")

    # -- 遍历在数据源的尽头停下，而不是在数据源宣称的条数上停下 --------------------------
    wide = MockGitHub(label="a")
    clock = FakeClock()
    limiter = RateLimiter(per_minute=PER_MINUTE, clock=clock.time, sleep=clock.sleep)
    first = start_backfill(
        session,
        collector=wide.collector,
        target_pages=MOCK_PAGE_COUNT + 2,
        per_page=MOCK_PER_PAGE,
        limiter=limiter,
        now=WALK_NOW,
    )
    first_repos = _repo_count(session, "a")

    # -- 一次中断只损失在飞的那一页，不损失历史 ------------------------------------------
    flaky = MockGitHub(label="b", failing_pages=(FAILING_PAGE,))
    fail_clock = FakeClock()
    fail_limiter = RateLimiter(per_minute=PER_MINUTE, clock=fail_clock.time, sleep=fail_clock.sleep)
    interrupted = ""
    interrupted_error = ""
    try:
        start_backfill(
            session,
            collector=flaky.collector,
            target_pages=MOCK_PAGE_COUNT,
            per_page=MOCK_PER_PAGE,
            limiter=fail_limiter,
            now=WALK_NOW,
        )
    except BackfillWalkFailed as exc:
        interrupted = str(exc)
        interrupted_error = exc.error_code
    else:
        raise RuntimeError("a walk whose page three failed reported success")
    failing_job = list_jobs(session, provider="github", limit=1)[0]
    failing_runs_before = _runs_for_job(session, failing_job.id)
    failing_run = failing_runs_before[-1]
    # 在这里就拍下来，不能往后拖：任务行就是游标，下面的续跑会把它推进。事后再读，
    # 等于拿续跑的进度去对续跑的预期，那么无论中断的遍历实际停在哪里都会通过。
    failed_state = summarize_job(failing_job)
    repos_after_failure = _repo_count(session, "b")
    attempts_on_failing_page = flaky.requests_for(FAILING_PAGE)

    # -- 续跑只走剩下的部分，也只走剩下的部分 -------------------------------------------
    flaky.open_page(FAILING_PAGE)
    mark = len(flaky.requests)
    resumed = resume_backfill(
        session,
        job_id=failing_job.id,
        collector=flaky.collector,
        # 刻意复用失败那次的同一个限流器对象：结果必须报出*这一趟*花了多少。去读限流器
        # 的累计总量，等于把失败尝试已经等过的时间记到续跑头上。
        limiter=fail_limiter,
        now=WALK_NOW + timedelta(minutes=1),
    )
    resumed_pages_requested = flaky.pages_requested_since(mark)
    repos_after_resume = _repo_count(session, "b")
    failing_runs_after = _runs_for_job(session, failing_job.id)
    resumed_state = summarize_job(failing_job)

    # -- 再走一遍同样的页，除任务自己的计数外不改任何行 ----------------------------------
    replay = MockGitHub(label="a")
    replay_outcome = start_backfill(
        session,
        collector=replay.collector,
        target_pages=MOCK_PAGE_COUNT,
        per_page=MOCK_PER_PAGE,
        limiter=RateLimiter(per_minute=PER_MINUTE, clock=FakeClock().time, sleep=lambda _: None),
        now=WALK_NOW,
    )
    repos_after_replay = _repo_count(session, "a")

    # -- 数据源行上的预算，就是遍历实际花掉的预算 ----------------------------------------
    source = ensure_data_source(session, wide.collector)
    budget_clock = FakeClock()
    budget_job = create_job(
        session,
        provider="github",
        query=wide.collector.describe_query(),
        target_pages=2,
        per_page=2,
        now=WALK_NOW,
    )
    budget_seen: list[int] = []
    original_limiter = backfill_service.RateLimiter

    def spy_limiter(*, per_minute: int) -> RateLimiter:
        """记下遍历推出来的预算，然后在假时钟上按它限速。"""
        budget_seen.append(per_minute)
        return original_limiter(
            per_minute=per_minute, clock=budget_clock.time, sleep=budget_clock.sleep
        )

    budget_collector = MockGitHub(label="c", page_count=6, total_count=6000)
    backfill_service.RateLimiter = spy_limiter  # type: ignore[assignment]
    try:
        budget_outcome = run_backfill(session, job=budget_job, collector=budget_collector.collector)
    finally:
        backfill_service.RateLimiter = original_limiter

    # -- 一个还活着的游标不能被认领两次 ------------------------------------------------
    guarded = create_job(
        session,
        provider="github",
        query={"q": SMOKE_QUERY},
        target_pages=3,
        per_page=2,
        now=WALK_NOW,
    )
    claim_job(session, guarded, now=WALK_NOW)
    busy = ""
    try:
        claim_job(session, guarded, now=WALK_NOW + timedelta(minutes=1))
    except BackfillJobBusy as exc:
        busy = exc.held_since
    else:
        raise RuntimeError("a job with a live heartbeat was claimed by a second walker")
    stale = claim_job(session, guarded, now=WALK_NOW + STALE_AFTER + timedelta(minutes=1))

    # -- 已结束的任务拒绝续跑 ----------------------------------------------------------
    not_resumable = ""
    try:
        resume_backfill(
            session,
            job_id=first.job_id,
            collector=wide.collector,
            limiter=limiter,
            now=WALK_NOW,
        )
    except BackfillNotResumable as exc:
        not_resumable = str(exc)
    else:
        raise RuntimeError("a completed walk was resumed")

    # 限定在本轮运行创建的任务上。一份还会把早先中断运行留下的行算进去的冒烟账本，
    # 会第二次运行失败、第一次却通过 —— 这是一项检查最没用的一种表现。
    smoke_job_ids = {
        first.job_id,
        failing_job.id,
        replay_outcome.job_id,
        budget_job.id,
        guarded.id,
    }
    smoke_job_refs = {str(job_id) for job_id in smoke_job_ids}
    audit = [
        entry
        for entry in session.scalars(
            select(AuditLog).where(AuditLog.action == "backfill.run").order_by(AuditLog.id)
        ).all()
        if entry.resource_id in smoke_job_refs
    ]
    jobs_after = int(session.scalar(select(func.count()).select_from(BackfillJob)) or 0)

    # -- 断言 ----------------------------------------------------------------------------
    if first.status != SUCCEEDED or not first.exhausted:
        raise RuntimeError(f"the wide walk did not finish cleanly: {first.status}")
    if first.pages_this_attempt != MOCK_PAGE_COUNT:
        raise RuntimeError(
            f"the wide walk requested {first.pages_this_attempt} pages, "
            f"but the source only serves {MOCK_PAGE_COUNT}"
        )
    if first.items_this_attempt != MOCK_PAGE_COUNT * MOCK_PER_PAGE:
        raise RuntimeError(f"the wide walk ingested {first.items_this_attempt} repositories")
    if first.next_page != MOCK_PAGE_COUNT + 1:
        raise RuntimeError(f"the cursor stopped at {first.next_page}")
    if clock.sleeps != [INTERVAL_SECONDS] * (MOCK_PAGE_COUNT - 1):
        raise RuntimeError(f"the wide walk paced {clock.sleeps}")
    if abs(first.paced_seconds - INTERVAL_SECONDS * (MOCK_PAGE_COUNT - 1)) > 1e-6:
        raise RuntimeError(f"the wide walk reported {first.paced_seconds} seconds of pacing")
    if first_repos != MOCK_PAGE_COUNT * MOCK_PER_PAGE:
        raise RuntimeError(f"the wide walk landed {first_repos} repository rows")

    if interrupted_error != "COLLECTOR_FAILED":
        raise RuntimeError(f"the failed walk was classified as {interrupted_error!r}")
    if failed_state.status != FAILED or failed_state.next_page != FAILING_PAGE:
        raise RuntimeError(
            f"the failed job stopped at {failed_state.status}/{failed_state.next_page}"
        )
    if failed_state.pages_completed != FAILING_PAGE - 1:
        raise RuntimeError(f"the failed job counted {failed_state.pages_completed} pages")
    if failed_state.items_collected != (FAILING_PAGE - 1) * MOCK_PER_PAGE:
        raise RuntimeError(f"the failed job counted {failed_state.items_collected} repositories")
    if failed_state.pages_remaining != MOCK_PAGE_COUNT - (FAILING_PAGE - 1):
        raise RuntimeError(f"the failed job reports {failed_state.pages_remaining} pages remaining")
    if not failed_state.resumable or failed_state.attempt != 1:
        raise RuntimeError("the failed job does not look like a resumable first attempt")
    if attempts_on_failing_page != 3:
        raise RuntimeError(
            f"page {FAILING_PAGE} was requested {attempts_on_failing_page} times; "
            "the retry policy should have tried three"
        )
    if repos_after_failure != (FAILING_PAGE - 1) * MOCK_PER_PAGE:
        raise RuntimeError(f"the interrupted walk landed {repos_after_failure} repository rows")
    if len(failing_runs_before) != 1:
        raise RuntimeError(
            f"the interrupted attempt left {len(failing_runs_before)} run rows; expected one"
        )
    if failing_run.status != "failed" or failing_run.error_code != "COLLECTOR_FAILED":
        raise RuntimeError(
            f"the failed walk's run row reads {failing_run.status}/{failing_run.error_code}"
        )
    if (failing_run.metrics_json or {}).get("next_page") != FAILING_PAGE:
        raise RuntimeError("the failed walk's run row does not record where the cursor stopped")

    if resumed_pages_requested != [FAILING_PAGE, FAILING_PAGE + 1]:
        raise RuntimeError(f"the resume requested pages {resumed_pages_requested}")
    if resumed.attempt != 2 or resumed.started_page != FAILING_PAGE:
        raise RuntimeError(
            f"the resume started at page {resumed.started_page} as attempt {resumed.attempt}"
        )
    if resumed.pages_completed != MOCK_PAGE_COUNT:
        raise RuntimeError(f"the resume finished {resumed.pages_completed} pages")
    if resumed.items_collected != MOCK_PAGE_COUNT * MOCK_PER_PAGE:
        raise RuntimeError(f"the resume counted {resumed.items_collected} repositories")
    if not resumed.exhausted:
        raise RuntimeError("the resume did not reach the end of the source")
    if fail_clock.sleeps != [
        INTERVAL_SECONDS,
        INTERVAL_SECONDS,
        INTERVAL_SECONDS,
        INTERVAL_SECONDS,
    ]:
        raise RuntimeError(f"the two attempts paced {fail_clock.sleeps}")
    if abs(resumed.paced_seconds - 2 * INTERVAL_SECONDS) > 1e-6:
        raise RuntimeError(
            f"the resume reported {resumed.paced_seconds} seconds of pacing; "
            "it must count only its own two intervals"
        )
    if repos_after_resume != MOCK_PAGE_COUNT * MOCK_PER_PAGE:
        raise RuntimeError(f"the resumed walk landed {repos_after_resume} repository rows")
    if resumed_state.status != SUCCEEDED or resumed_state.resumable:
        raise RuntimeError(
            f"after the resume the job reads {resumed_state.status} "
            f"with resumable={resumed_state.resumable}"
        )
    if resumed_state.attempt != 2 or resumed_state.pages_completed != MOCK_PAGE_COUNT:
        raise RuntimeError(
            f"the job's own counters read attempt={resumed_state.attempt} "
            f"pages={resumed_state.pages_completed} after the resume"
        )
    if len(failing_runs_after) != 2:
        raise RuntimeError(
            f"the interrupted job raised {len(failing_runs_after)} run rows in total; "
            "each attempt is supposed to leave one"
        )
    if [run.status for run in failing_runs_after] != ["failed", "succeeded"]:
        raise RuntimeError(f"the job's run rows read {[run.status for run in failing_runs_after]}")

    if replay_outcome.items_this_attempt != MOCK_PAGE_COUNT * MOCK_PER_PAGE:
        raise RuntimeError("the replay did not walk the same pages")
    if repos_after_replay != first_repos:
        raise RuntimeError(
            f"replaying the walk changed the repository count from {first_repos} "
            f"to {repos_after_replay}; ingestion is not idempotent"
        )

    if budget_seen != [source.rate_limit_per_minute]:
        raise RuntimeError(
            f"the walk built limiters for {budget_seen}; expected the source's "
            f"{source.rate_limit_per_minute}"
        )
    if budget_outcome.pages_this_attempt != 2 or budget_outcome.next_page != 3:
        raise RuntimeError("the budgeted walk did not stop on its page budget")
    if budget_outcome.exhausted:
        raise RuntimeError("a walk that stopped on its budget claimed to have exhausted the source")

    if not busy:
        raise RuntimeError("the busy guard did not report when the cursor was held")
    if stale.attempt != 2:
        raise RuntimeError("a stale heartbeat did not hand the cursor to the next walker")
    if "already completed" not in not_resumable:
        raise RuntimeError(f"resuming a finished walk said {not_resumable!r}")
    if [entry.result for entry in audit] != [
        "success",
        "failed",
        "success",
        "success",
        "success",
    ]:
        raise RuntimeError(f"the audit trail reads {[entry.result for entry in audit]}")
    if jobs_after - jobs_before != 5:
        raise RuntimeError(
            f"the run created {jobs_after - jobs_before} jobs; "
            "expected five and four refused attempts that wrote nothing"
        )

    return {
        "unsupported_collector": unsupported,
        "bound_refusals": bounds,
        "wide_pages_requested": wide.pages_requested(),
        "wide_exhausted": first.exhausted,
        "wide_next_page": first.next_page,
        "wide_items": first.items_this_attempt,
        "wide_sleeps": clock.sleeps,
        "wide_paced_seconds": first.paced_seconds,
        "wide_repository_rows": first_repos,
        "failed_error_code": interrupted_error,
        "failed_page": FAILING_PAGE,
        "failed_next_page": failed_state.next_page,
        "failed_pages_completed": failed_state.pages_completed,
        "failed_items_collected": failed_state.items_collected,
        "failed_pages_remaining": failed_state.pages_remaining,
        "failed_resumable": failed_state.resumable,
        "attempts_on_failing_page": attempts_on_failing_page,
        "repository_rows_after_failure": repos_after_failure,
        "failed_run_status": failing_run.status,
        "failed_run_error_code": failing_run.error_code,
        "runs_before_resume": len(failing_runs_before),
        "resumed_pages_requested": resumed_pages_requested,
        "resumed_attempt": resumed.attempt,
        "resumed_started_page": resumed.started_page,
        "resumed_pages_completed": resumed.pages_completed,
        "resumed_items_collected": resumed.items_collected,
        "resumed_exhausted": resumed.exhausted,
        "resumed_paced_seconds": resumed.paced_seconds,
        "resumed_job_status": resumed_state.status,
        "resumed_job_attempt": resumed_state.attempt,
        "resumed_job_pages_completed": resumed_state.pages_completed,
        "resumed_job_resumable": resumed_state.resumable,
        "runs_after_resume": len(failing_runs_after),
        "run_statuses_after_resume": [run.status for run in failing_runs_after],
        "sleeps_across_attempts": fail_clock.sleeps,
        "repository_rows_after_resume": repos_after_resume,
        "repository_rows_after_replay": repos_after_replay,
        "source_rate_limit_per_minute": source.rate_limit_per_minute,
        "derived_limiter_budgets": budget_seen,
        "budget_walk_pages": budget_outcome.pages_this_attempt,
        "budget_walk_exhausted": budget_outcome.exhausted,
        "busy_held_since": busy,
        "stale_takeover_attempt": stale.attempt,
        "not_resumable_message": not_resumable,
        "audit_results": [entry.result for entry in audit],
        "jobs_created": jobs_after - jobs_before,
        "first_job_id": first.job_id,
        "failed_job_id": failing_job.id,
        "interrupted_message": interrupted,
    }


# --------------------------------------------------------------------------------------
# 第二部分：周报
# --------------------------------------------------------------------------------------


def _cache_readmes(session: Session) -> int:
    """给本冒烟落下的每个仓库补一份缓存 README。

    关于某个仓库的报告小节，没有证据就无法通过校验，而 Agent 的正文工具唯一认的证据，
    是采集器已经缓存下来的内容。真实部署靠抓取 readme 填上这些；这里直接写入行，
    也是本脚本里唯一一处冒烟替流水线阶段写数据的地方。
    """
    repositories = session.scalars(
        select(Repository).where(
            Repository.provider_repo_id.startswith(REPO_PREFIX, autoescape=True)
        )
    ).all()
    written = 0
    for repository in repositories:
        existing = session.scalar(
            select(RepositoryContent).where(
                RepositoryContent.repository_id == repository.id,
                RepositoryContent.content_type == "readme",
            )
        )
        if existing is not None:
            continue
        session.add(
            RepositoryContent(
                repository_id=repository.id,
                content_type="readme",
                source_url=f"https://example.com/{repository.full_name}/readme",
                content=(
                    f"# {repository.full_name}\n\n"
                    f"A {repository.language} project tracked by the T16 smoke check.\n"
                    f"It matches {KEYWORD} and has {repository.stars} stars.\n"
                ),
                content_hash=f"{REPO_PREFIX}{repository.id}",
                fetched_at=BUSINESS_NOW,
            )
        )
        written += 1
    session.commit()
    return written


def _weekly_report_checks(session: Session, ids: dict[str, int]) -> dict[str, Any]:
    """跑 collect -> snapshot -> rank -> research -> compose -> verify ->
    review -> publish。"""
    settings = get_settings()
    topic_id = ids["topic_id"]

    daily = run_daily_pipeline(
        session,
        business_date=BUSINESS_DATE,
        collectors=[MockGitHub(label="daily").collector],
        limit=DAILY_LIMIT,
        attempt=1,
    )
    stages = [(stage.run_type, stage.status) for stage in daily.stages]
    candidates = list(
        session.scalars(
            select(TopicRepository)
            .where(TopicRepository.topic_id == topic_id)
            .order_by(TopicRepository.rank)
        ).all()
    )
    selected = [link for link in candidates if link.selected_for_agent]
    readmes = _cache_readmes(session)

    agent = run_research_agent(
        session,
        topic_id=topic_id,
        settings=settings,
        max_candidates=AGENT_CANDIDATES,
        now=BUSINESS_NOW,
    )
    generated = generate_report(
        session,
        agent_run_id=agent.agent_run_id,
        now=BUSINESS_NOW,
    )
    verification = verify_report(
        session,
        report_id=generated.report_id,
        settings=settings,
        now=BUSINESS_NOW,
    )
    decision = publish_or_request_review(
        session,
        report_id=generated.report_id,
        settings=settings,
        now=BUSINESS_NOW,
    )
    review = record_review(
        session,
        report_id=generated.report_id,
        decision="approve",
        reviewer_id=ids["user_id"],
        comment="T16 smoke approval",
        now=BUSINESS_NOW,
    )
    released = publish_due_reports(session, now=BUSINESS_NOW)
    report = session.get(Report, generated.report_id)
    if report is None:
        raise RuntimeError("the promoted report row disappeared")

    # -- 断言 ----------------------------------------------------------------------------
    if daily.status != "succeeded":
        raise RuntimeError(f"the daily pipeline closed as {daily.status}")
    for run_type in ("collect", "snapshot", "rank"):
        if (run_type, "succeeded") not in stages:
            raise RuntimeError(f"the {run_type} stage did not succeed: {stages}")
    if daily.provider_errors:
        raise RuntimeError(f"the collect stage reported {daily.provider_errors}")
    if daily.provider_counts != {"github": DAILY_LIMIT}:
        raise RuntimeError(f"the collect stage counted {daily.provider_counts}")
    if daily.snapshot_count < DAILY_LIMIT + 1:
        raise RuntimeError(f"the snapshot stage only covered {daily.snapshot_count} repositories")
    if daily.topic_candidate_counts.get(str(topic_id), 0) < DAILY_LIMIT:
        raise RuntimeError(f"ranking found {daily.topic_candidate_counts} candidates")
    if not selected:
        raise RuntimeError("no candidate was selected for the Agent")
    if len(selected) != AGENT_CANDIDATES:
        raise RuntimeError(
            f"the topic selected {len(selected)} candidates; the Agent reads exactly "
            f"{AGENT_CANDIDATES} before its tool budget runs out"
        )
    if len(candidates) <= AGENT_CANDIDATES:
        raise RuntimeError(
            f"only {len(candidates)} repositories matched the topic, so the cap was never hit"
        )
    if [link.rank for link in candidates] != list(range(1, len(candidates) + 1)):
        raise RuntimeError("candidate ranks are not contiguous")
    if readmes < 1:
        raise RuntimeError("no readme was cached for the Agent to read")

    if agent.status != "completed" or agent.error_code is not None:
        raise RuntimeError(f"the research run closed as {agent.status}/{agent.error_code}")
    if agent.budget_degraded or agent.guard_triggered:
        raise RuntimeError(
            f"the research run was degraded: budget={agent.budget_degraded} "
            f"guard={agent.guard_triggered}"
        )
    if agent.section_count != 6 or agent.report_draft_id is None:
        raise RuntimeError(f"the draft has {agent.section_count} sections")
    if agent.verification is None or not agent.verification.sufficient:
        raise RuntimeError("the Agent's own evidence check did not pass")
    if not agent.verification.content_repo_ids:
        raise RuntimeError("the Agent read no cached repository content")

    if not generated.created or generated.version != 1:
        raise RuntimeError(
            f"the first promotion reported created={generated.created} version={generated.version}"
        )
    if generated.source_count < 6:
        raise RuntimeError(f"the report carries {generated.source_count} citation rows")
    if not verification.passed:
        raise RuntimeError(f"the verifier rejected the report: {verification.reason_codes}")

    if decision.status != "pending_review" or not decision.requires_review:
        raise RuntimeError(f"the policy resolved to {decision.status}")
    if "FIRST_RUN" not in decision.reason_codes:
        raise RuntimeError(f"the policy reasons read {decision.reason_codes}")
    if review.status != "approved":
        raise RuntimeError(f"the approval left the report as {review.status}")

    mine = [entry for entry in released if entry["report_id"] == generated.report_id]
    if len(mine) != 1 or mine[0]["status"] != "published":
        raise RuntimeError(f"the release sweep reported {mine}")
    if report.status != "published" or report.published_at is None:
        raise RuntimeError(f"the report reads {report.status} after the release sweep")
    if report.published_at != BUSINESS_NOW:
        raise RuntimeError(f"the report was published at {report.published_at}")

    return {
        "daily_status": daily.status,
        "daily_stages": stages,
        "daily_provider_counts": daily.provider_counts,
        "daily_snapshot_count": daily.snapshot_count,
        "daily_candidate_counts": daily.topic_candidate_counts,
        "candidate_rows": len(candidates),
        "selected_rows": len(selected),
        "readmes_cached": readmes,
        "agent_status": agent.status,
        "agent_section_count": agent.section_count,
        "agent_tool_calls": agent.tool_call_count,
        "agent_model_calls": agent.model_call_count,
        "agent_evidence": agent.evidence_count,
        "agent_budget_degraded": agent.budget_degraded,
        "agent_guard_triggered": agent.guard_triggered,
        "agent_content_repos": len(agent.verification.content_repo_ids)
        if agent.verification
        else 0,
        "agent_prior_baseline": agent.verification.has_prior_baseline
        if agent.verification
        else False,
        "report_id": generated.report_id,
        "report_version": generated.version,
        "report_source_count": generated.source_count,
        "verification_passed": verification.passed,
        "verification_reasons": list(verification.reason_codes),
        "decision_status": decision.status,
        "decision_reasons": list(decision.reason_codes),
        "review_status": review.status,
        "released_count": len(released),
        "report_status": report.status,
        "report_published_at": report.published_at.isoformat() if report.published_at else None,
        "report_title": report.title,
    }


# --------------------------------------------------------------------------------------
# 真正的 CLI，以子进程方式运行
# --------------------------------------------------------------------------------------


def _run_cli(*args: str) -> dict[str, Any]:
    """跑一次真正的回填 CLI 调用，并解析它的 JSON 结果。"""
    completed = subprocess.run(
        [sys.executable, "-m", "open_source_intelligence.backfill.cli", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"backfill CLI {' '.join(args)} failed: "
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _run_cli_expecting_failure(*args: str) -> tuple[int, str]:
    """跑一次必须被拒的 CLI 调用，并返回它的退出码与消息。"""
    completed = subprocess.run(
        [sys.executable, "-m", "open_source_intelligence.backfill.cli", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        raise RuntimeError(f"backfill CLI {' '.join(args)} unexpectedly succeeded")
    return completed.returncode, (completed.stderr or completed.stdout).strip()


def _cli_checks(job_id: int) -> dict[str, Any]:
    """把真正的 CLI 当子进程驱动：读类动作，以及两次必须被拒的调用。"""
    listed = _run_cli("--action", "list", "--provider", "github", "--limit", "5")
    status = _run_cli("--action", "status", "--job-id", str(job_id))
    over_budget = _run_cli_expecting_failure(
        "--action", "start", "--provider", "github", "--target-pages", "500"
    )
    already_done = _run_cli_expecting_failure("--action", "resume", "--job-id", str(job_id))

    jobs = listed.get("jobs", [])
    if not jobs:
        raise RuntimeError("the CLI listed no backfill jobs")
    if any(job["provider"] != "github" for job in jobs):
        raise RuntimeError("the CLI listed a job from another provider")
    if status.get("job", {}).get("job_id") != job_id:
        raise RuntimeError(f"the CLI reported {status} for job {job_id}")
    if status["job"]["resumable"]:
        raise RuntimeError("the CLI calls a finished job resumable")
    if "between 1 and" not in over_budget[1]:
        raise RuntimeError(f"the page budget refusal reads {over_budget[1]!r}")
    if "already completed" not in already_done[1]:
        raise RuntimeError(f"the resume refusal reads {already_done[1]!r}")

    return {
        "listed_jobs": len(jobs),
        "listed_first_job": jobs[0]["job_id"],
        "status_job": status["job"]["job_id"],
        "status_status": status["job"]["status"],
        "status_resumable": status["job"]["resumable"],
        "over_budget_exit_code": over_budget[0],
        "over_budget_message": over_budget[1],
        "already_done_exit_code": already_done[0],
        "already_done_message": already_done[1],
    }


# --------------------------------------------------------------------------------------
# 用全新会话回读
# --------------------------------------------------------------------------------------


def readback(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """经一个什么都没写过的会话，把落库的行重新读一遍。

    上面每一条断言都跑在做事的那个会话里，而在那里，一份陈旧的标识映射能让尚未提交的
    改动看起来已经提交。这一趟直接问数据库。
    """
    backfill = payload["backfill"]
    report = payload["report"]

    finished_job = session.get(BackfillJob, backfill["first_job_id"])
    if finished_job is None:
        raise RuntimeError("the finished backfill job is not readable")
    failed_job = session.get(BackfillJob, backfill["failed_job_id"])
    if failed_job is None:
        raise RuntimeError("the interrupted backfill job is not readable")
    stored_report = session.get(Report, report["report_id"])
    if stored_report is None:
        raise RuntimeError("the published report is not readable")

    runs = list(
        session.scalars(
            select(PipelineRun).where(PipelineRun.run_type == "backfill").order_by(PipelineRun.id)
        ).all()
    )
    if runs and runs[-1].status != "succeeded":
        raise RuntimeError(f"the last backfill run reads {runs[-1].status}")
    resumed_runs = [
        run
        for run in runs
        if (run.metrics_json or {}).get("backfill_job_id") == backfill["failed_job_id"]
    ]
    verification = session.scalar(
        select(ReportVerification).where(
            ReportVerification.report_id == stored_report.id,
            ReportVerification.version == stored_report.current_version,
        )
    )
    approval = session.scalar(
        select(ReportReview).where(
            ReportReview.report_id == stored_report.id,
            ReportReview.decision == "approve",
        )
    )
    version = session.scalar(
        select(ReportVersion).where(
            ReportVersion.report_id == stored_report.id,
            ReportVersion.version == stored_report.current_version,
        )
    )
    sources = int(
        session.scalar(
            select(func.count())
            .select_from(ReportSource)
            .where(
                ReportSource.report_id == stored_report.id,
                ReportSource.version == stored_report.current_version,
            )
        )
        or 0
    )

    if finished_job.status != SUCCEEDED:
        raise RuntimeError(f"the finished job reads {finished_job.status} on re-read")
    if failed_job.next_page != MOCK_PAGE_COUNT + 1 or failed_job.attempt != 2:
        raise RuntimeError(
            f"the resumed job reads next_page={failed_job.next_page} attempt={failed_job.attempt} "
            "on re-read; the two attempts should have converged on the end of the source"
        )
    if [run.status for run in resumed_runs] != ["failed", "succeeded"]:
        raise RuntimeError(
            f"the resumed job's run rows read {[run.status for run in resumed_runs]} on re-read"
        )
    if stored_report.status != "published":
        raise RuntimeError(f"the report reads {stored_report.status} on re-read")
    if verification is None or verification.verdict != "passed":
        raise RuntimeError("the passing verdict did not survive the commit")
    if approval is None:
        raise RuntimeError("the approval did not survive the commit")
    if version is None:
        raise RuntimeError("the report version row did not survive the commit")
    if sources < 6:
        raise RuntimeError(f"only {sources} citation rows survived the commit")

    return {
        "finished_job_status": finished_job.status,
        "finished_job_next_page": finished_job.next_page,
        "finished_job_items": finished_job.items_collected,
        "resumed_job_status": failed_job.status,
        "resumed_job_attempt": failed_job.attempt,
        "resumed_job_next_page": failed_job.next_page,
        "resumed_job_items": failed_job.items_collected,
        "resumed_run_statuses": [run.status for run in resumed_runs],
        "backfill_runs": len(runs),
        "report_status": stored_report.status,
        "report_version": stored_report.current_version,
        "verification_verdict": verification.verdict,
        "approval_decision": approval.decision,
        "version_row": version.id is not None,
        "citation_rows": sources,
    }


# --------------------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    """解析验证脚本使用的可选清理模式。"""
    parser = argparse.ArgumentParser(description="Run T16 backfill and weekly report smoke checks")
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Only remove T16 smoke records and exit",
    )
    return parser.parse_args()


def main() -> None:
    """在真实 MySQL 上跑 T16 冒烟，并打印断言结果。"""
    args = _parse_args()
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    if args.cleanup_only:
        with session_factory() as session:
            removed = cleanup_verification_records(session)
        engine.dispose()
        print(json.dumps({"cleanup_only": True, "removed": removed}, ensure_ascii=False))
        return

    payload: dict[str, Any] = {}
    try:
        with session_factory() as session:
            cleanup_verification_records(session)
            ids = seed_topic(session)
            payload = {
                "backfill": _backfill_checks(session),
                "report": _weekly_report_checks(session, ids),
            }
            payload["cli"] = _cli_checks(payload["backfill"]["first_job_id"])
            payload["topic"] = {"topic_id": ids["topic_id"], "user_id": ids["user_id"]}
        with session_factory() as session:
            payload["readback"] = readback(session, payload)
    finally:
        for mock in _MOCKS:
            mock.close()
        with session_factory() as session:
            payload["removed"] = cleanup_verification_records(session)
        engine.dispose()

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
