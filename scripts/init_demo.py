"""用一次幂等执行，把空部署变成一份已发布的演示报告。

`scripts/seed_demo.py` 只做到候选层为止 —— 账号、数据源、主题、排名后的仓库、
快照、README 缓存、推送目标，以及评估数据集特意留作停用的两种边界形态。
那是数据；本脚本在数据之上驱动*流水线*，好让控制台一打开就看到一份真实、
已发布的报告，而不是空账本。

这一趟用离线模型（`AGENT_PROVIDER=offline`）跑确定性的那一半，因此既不需要
网络也不需要凭据，每次产出的报告都一模一样。它**不**跑 `run_daily_pipeline`
（collect → snapshot → rank → contents）：种子已经手工写好了那些行，重新采集
还得有可达的数据源。它跑的是排名之后的全部环节，而这正是演示必须证明的东西：

    research (offline Agent) → generate → verify → approve → publish → auto-push

由于 `AUTO_WITH_DELAY` 报告要排在撤回窗口之后，脚本先记下人工 `approve`，
再把它放行 —— 这正是文档里操作员走的那条路，所以演示展示的是一份老老实实
走到 `published` 的报告，而不是靠绕过检查得来的。

它的幂等含义与种子脚本相同：每个阶段都复用上一轮建好的行，因此重跑不会推进
任何状态，也不会凭空多出一份。如果该主题已经有 `published` 报告，这一趟就
报告它并直接返回，不再新建 agent 运行或报告。在种子脚本之后、于 api 容器内
运行：

    docker compose --env-file .env -f infra/docker-compose.yml exec -T api \
      python /app/scripts/init_demo.py
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

# `scripts/` 在容器内是以文件方式执行的（`python /app/scripts/init_demo.py`），此时
# `scripts` 并不是已安装的包；把同级的种子脚本按名字变成可导入的。
sys.path.insert(0, str(Path(__file__).resolve().parent))

import seed_demo as seed  # noqa: E402

from open_source_intelligence.agent.runtime import run_research_agent
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.reports.composition import generate_report
from open_source_intelligence.reports.publishing import (
    publish_due_reports,
    publish_or_request_review,
    record_review,
)
from open_source_intelligence.security.service import get_user_by_username

_ADMIN_PASSWORD = os.environ.get("SEED_ADMIN_PASSWORD", "dev-admin-password-change-me")
_USER_PASSWORD = os.environ.get("SEED_USER_PASSWORD", "dev-user-password-change-me")


def _offline_settings():
    """返回部署配置的副本，把模型钉成离线那档，好让本次运行可复现。"""
    base = get_settings()
    return base.model_copy(
        update={"agent_provider": "offline", "agent_model": "offline-deterministic-v1"}
    )


def _has_published_report(session, topic_id: int) -> Report | None:
    """返回该主题最近一份 `published` 报告；没有就返回 `None`。

    这是幂等闸门：已经走到 `published` 的主题没什么可再演示的了，再把
    research → publish 这一圈走一遍，只会为同一个窗口凭空多出一次 agent 运行
    和一份报告。
    """
    return session.scalar(
        select(Report)
        .where(Report.topic_id == topic_id, Report.status == "published")
        .order_by(Report.id.desc())
        .limit(1)
    )


def main() -> int:
    """先播种，再把 research → publish 这一圈推到产出一份已发布报告。

    幂等：如果该主题已有 `published` 报告，这一趟就报告它并直接返回，不再新建
    agent 运行、草稿或报告。对一个健康部署重跑不会推进任何状态 —— 与种子脚本
    给出的是同一个保证。
    """
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)
    offline = _offline_settings()
    try:
        with factory() as session:
            # --- 播种 ---------------------------------------------------------
            seed._ensure_accounts(  # noqa: SLF001
                session,
                admin_password=_ADMIN_PASSWORD,
                user_password=_USER_PASSWORD,
            )
            admin = get_user_by_username(session, "admin")
            assert admin is not None
            source = seed._ensure_source(session)  # noqa: SLF001
            topic = seed._ensure_topic(session, admin)  # noqa: SLF001
            seed._ensure_repositories(session, source, topic)  # noqa: SLF001
            seed._ensure_push_targets(session)  # noqa: SLF001

            # --- 幂等闸门 -----------------------------------------------------
            published = _has_published_report(session, topic.id)
            if published is not None:
                print(
                    f"idempotent: topic {topic.id} already has a published report "
                    f"(report={published.id}); nothing to do"
                )
                return 0

            # --- 研究（离线） -------------------------------------------------
            result = run_research_agent(session, topic_id=topic.id, settings=offline)
            print(f"research: agent_run={result.agent_run_id} status={result.status}")
            if result.agent_run_id is None:
                print(f"research produced no run: {result.to_dict()}")
                return 1

            # --- 生成 --------------------------------------------------------
            report = session.scalar(
                select(Report).where(Report.agent_run_id == result.agent_run_id)
            )
            if report is None:
                generated = generate_report(session, agent_run_id=result.agent_run_id)
                report = session.get(Report, generated.report_id)
            assert report is not None
            print(
                f"generate: report={report.id} version={report.current_version} "
                f"status={report.status}"
            )

            # --- 校验 → 审批 → 发布 → 放行 -----------------------------------
            if report.status in {"draft", "pending_review", "scheduled"}:
                publish_or_request_review(session, report_id=report.id, settings=offline)
                session.expire_all()
                report = session.get(Report, report.id)
                assert report is not None
                print(f"publish policy: status={report.status}")

            if report.status == "pending_review":
                record_review(
                    session,
                    report_id=report.id,
                    decision="approve",
                    reviewer_id=admin.id,
                    comment="演示数据：一键初始化自动审核通过",
                )
                session.expire_all()
                report = session.get(Report, report.id)
                assert report is not None
                print(f"review: status={report.status}")

            if report.status == "approved":
                publish_or_request_review(session, report_id=report.id, settings=offline)
                session.expire_all()
                report = session.get(Report, report.id)
                assert report is not None
                print(f"queue: status={report.status}")

            if report.status == "scheduled":
                # `AUTO_WITH_DELAY` 会把报告停在撤回窗口之后。演示必须展示终态，所以用
                # 一只越过窗口的时钟放行 —— 与发布测试用的是同一种 `now` 注入。
                released = publish_due_reports(
                    session,
                    now=utc_now() + timedelta(hours=2),
                    limit=10,
                )
                session.expire_all()
                report = session.get(Report, report.id)
                assert report is not None
                print(f"release: released={len(released)} status={report.status}")

            print(
                f"final: report={report.id} status={report.status} version={report.current_version}"
            )
            return 0 if report.status == "published" else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
