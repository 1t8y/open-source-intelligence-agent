"""演示数据播种脚本必须幂等，并且对它的两种边界形态说实话。

`scripts/seed_demo.py` 把空库变成可演示的状态。有两条性质值得写进测试，
而不是靠手动跑一遍来相信：

1. **幂等** —— 每个实体在创建前都会先查一遍，所以第二次运行不能多出一行。
   这个播种脚本本来就要对着线上部署反复运行，而一个重跑就重复写入的播种脚本，
   是随时会把候选预算吹大的隐患。

2. **边界形态是真的** —— T25 评估数据集有两个用例保持停用，因为当前部署
   还造不出它们需要的数据形态。造出来的正是这个播种脚本：
   `example/readme-missing` 必须*没有* `repository_contents` 行，
   `example/prompt-injection` 的缓存 README 必须带着指令形状的文本。
   任一处漂移，评估就会重新启用一个用例，而它面对的数据
   其实并没有走到用例所说的那条边界。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import scripts.seed_demo as seed
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    RepositoryContent,
    RepoSnapshot,
    TopicRepository,
)
from open_source_intelligence.persistence.models.notifications import PushTarget


def _count(session: Session, model: type) -> int:
    """返回某个模型的行数。"""
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_seeding_is_idempotent(session: Session) -> None:
    """证明在同一个库上再跑一遍不会多出任何行。"""
    seed._ensure_accounts(  # noqa: SLF001
        session,
        admin_password="admin-password-123",
        user_password="user-password-123",
    )
    admin_user = seed.get_user_by_username(session, "admin")
    assert admin_user is not None
    source = seed._ensure_source(session)  # noqa: SLF001
    topic = seed._ensure_topic(session, admin_user)  # noqa: SLF001

    seed._ensure_repositories(session, source, topic)  # noqa: SLF001
    seed._ensure_push_targets(session)  # noqa: SLF001

    repos = _count(session, Repository)
    snapshots = _count(session, RepoSnapshot)
    contents = _count(session, RepositoryContent)
    links = _count(session, TopicRepository)
    targets = _count(session, PushTarget)

    # 第二遍：什么都不能变。
    seed._ensure_repositories(session, source, topic)  # noqa: SLF001
    seed._ensure_push_targets(session)  # noqa: SLF001
    session.commit()

    assert _count(session, Repository) == repos
    assert _count(session, RepoSnapshot) == snapshots
    assert _count(session, RepositoryContent) == contents
    assert _count(session, TopicRepository) == links
    assert _count(session, PushTarget) == targets


def test_boundary_shapes_are_real(session: Session) -> None:
    """证明那两个停用的评估用例确实按文档所述的形态被造了出来。"""
    admin = seed.get_user_by_username(session, "admin")
    if admin is None:
        seed._ensure_accounts(  # noqa: SLF001
            session,
            admin_password="admin-password-123",
            user_password="user-password-123",
        )
        admin = seed.get_user_by_username(session, "admin")
    assert admin is not None
    source = seed._ensure_source(session)  # noqa: SLF001
    topic = seed._ensure_topic(session, admin)  # noqa: SLF001
    seed._ensure_repositories(session, source, topic)  # noqa: SLF001

    missing = session.scalar(
        select(Repository).where(Repository.full_name == "example/readme-missing")
    )
    assert missing is not None
    assert (
        session.scalar(
            select(RepositoryContent).where(RepositoryContent.repository_id == missing.id)
        )
        is None
    )

    injected = session.scalar(
        select(Repository).where(Repository.full_name == "example/prompt-injection")
    )
    assert injected is not None
    content = session.scalar(
        select(RepositoryContent).where(RepositoryContent.repository_id == injected.id)
    )
    assert content is not None
    assert "Ignore all previous instructions" in content.content
    assert "忽略之前的所有指令" in content.content


def test_seed_writes_the_full_loop_shape(session: Session) -> None:
    """证明播种脚本写出了完整闭环：主题、排名候选、快照与数据源。"""
    admin = seed.get_user_by_username(session, "admin")
    if admin is None:
        seed._ensure_accounts(  # noqa: SLF001
            session,
            admin_password="admin-password-123",
            user_password="user-password-123",
        )
        admin = seed.get_user_by_username(session, "admin")
    assert admin is not None
    source = seed._ensure_source(session)  # noqa: SLF001
    topic = seed._ensure_topic(session, admin)  # noqa: SLF001
    seed._ensure_repositories(session, source, topic)  # noqa: SLF001

    assert source.source_type == "github"
    assert topic.name == "sweep-llm-agent-topic"
    assert topic.enabled is True

    links = session.scalars(
        select(TopicRepository)
        .where(TopicRepository.topic_id == topic.id)
        .order_by(TopicRepository.rank)
    ).all()
    assert [link.rank for link in links] == [1, 2, 3, 4, 5]
    # 三个正常候选是研究对象；两个边界形态不是。
    assert [link.selected_for_agent for link in links] == [True, True, True, False, False]

    # 任何候选都不带伪造的快照序列。播种脚本哪怕只写一个点，
    # 也会把一个谁都无法从 GitHub 复现的星数塞进时间线，之后真实采集
    # 就会跨在「伪造→真实」的边界上 —— 这正是 A1 要消除的缺陷。
    # 序列该由采集器来写。
    for repo in session.scalars(select(Repository)).all():
        snap = session.scalars(
            select(RepoSnapshot).where(RepoSnapshot.repository_id == repo.id)
        ).all()
        assert snap == []


def test_collected_repository_is_bound_not_rewritten(session: Session) -> None:
    """证明采集器拥有的仓库保留自己的计数器、历史与 README。

    这就是把「混库」缺陷写成一个测试。播种脚本原先按 `provider_repo_id` 查候选，
    而它自己写的是 `owner/name`；采集来的 GitHub 行带的却是数字 provider id，
    于是查询落空，播种脚本给同一个项目插入了*第二*行，
    还配上一套伪造的星数序列与快照历史。控制台随后给 `morluto/rea`
    显示出两个不同的星数 —— 采集的 370,108 与播种的 285,395 ——
    而一个主题把两者都排了进去。
    """
    admin = seed.get_user_by_username(session, "admin")
    if admin is None:
        seed._ensure_accounts(  # noqa: SLF001
            session,
            admin_password="admin-password-123",
            user_password="user-password-123",
        )
        admin = seed.get_user_by_username(session, "admin")
    assert admin is not None
    source = seed._ensure_source(session)  # noqa: SLF001
    topic = seed._ensure_topic(session, admin)  # noqa: SLF001

    collected = Repository(
        source_id=source.id,
        provider_repo_id="1209966933",
        full_name="morluto/rea",
        html_url="https://github.com/morluto/rea",
        description="collected",
        language="Python",
        stars=370108,
        forks=3120,
        first_seen_at=datetime(2026, 10, 20, tzinfo=UTC),
        last_seen_at=datetime(2026, 10, 23, tzinfo=UTC),
    )
    session.add(collected)
    session.flush()
    session.add(
        RepositoryContent(
            repository_id=collected.id,
            content_type="readme",
            source_url="https://github.com/morluto/rea/blob/main/README.md",
            content="# rea\n\nCollected README body.",
            content_hash="collected",
        )
    )
    for offset in range(4):
        session.add(
            RepoSnapshot(
                repository_id=collected.id,
                snapshot_date=date(2026, 10, 20) + timedelta(days=offset),
                stars=370100 + offset,
                forks=3120,
                stars_delta=1,
                forks_delta=0,
            )
        )
    session.commit()

    seed._ensure_repositories(session, source, topic)  # noqa: SLF001
    session.commit()

    rows = session.scalars(select(Repository).where(Repository.full_name == "morluto/rea")).all()
    assert len(rows) == 1, "the seeder created a second row for one repository"
    assert rows[0].id == collected.id
    assert rows[0].provider_repo_id == "1209966933"
    assert rows[0].stars == 370108, "the fixture series overwrote the collected counter"

    snapshots = session.scalars(
        select(RepoSnapshot).where(RepoSnapshot.repository_id == collected.id)
    ).all()
    assert len(snapshots) == 4, "the seeder wrote fixture snapshots over a collected series"

    content = session.scalar(
        select(RepositoryContent).where(RepositoryContent.repository_id == collected.id)
    )
    assert content is not None
    assert content.content == "# rea\n\nCollected README body."

    link = session.get(TopicRepository, (topic.id, collected.id))
    assert link is not None
    assert link.selected_for_agent is True
    assert link.manually_pinned is True


def test_seeding_leaves_no_shared_rank(session: Session) -> None:
    """证明残留的候选不会占据夹具同样占用的名次。

    一轮排名之后，主题上会留下它评过分的仓库，名次从 1 开始排。
    播种脚本接着给自己的候选写 1..5 名，于是残留项若正好排在 2，
    主题就有两个候选处在第 2 位 —— 此后每个按名次排序的读者
    （候选列表、报告的出处区块、Agent 的候选加载器）都会得到任意的顺序。
    """
    admin = seed.get_user_by_username(session, "admin")
    if admin is None:
        seed._ensure_accounts(  # noqa: SLF001
            session,
            admin_password="admin-password-123",
            user_password="user-password-123",
        )
        admin = seed.get_user_by_username(session, "admin")
    assert admin is not None
    source = seed._ensure_source(session)  # noqa: SLF001
    topic = seed._ensure_topic(session, admin)  # noqa: SLF001
    stale = Repository(
        source_id=source.id,
        provider_repo_id="555000111",
        full_name="example/leftover",
        html_url="https://github.com/example/leftover",
        description="leftover",
        language="Python",
        stars=10,
        forks=1,
        first_seen_at=datetime(2026, 10, 20, tzinfo=UTC),
        last_seen_at=datetime(2026, 10, 23, tzinfo=UTC),
    )
    session.add(stale)
    session.flush()
    session.add(
        TopicRepository(
            topic_id=topic.id,
            repository_id=stale.id,
            score=Decimal("1"),
            rank=2,
            selected_for_agent=True,
            manually_pinned=False,
        )
    )
    session.commit()

    seed._ensure_repositories(session, source, topic)  # noqa: SLF001
    session.commit()

    ranks = session.scalars(
        select(TopicRepository.rank).where(TopicRepository.topic_id == topic.id)
    ).all()
    leftover = session.get(TopicRepository, (topic.id, stale.id))

    assert len(ranks) == len(set(ranks)), f"two candidates share a position: {sorted(ranks)}"
    assert leftover is not None
    assert leftover.rank > len(seed.REPOSITORIES)


def test_accounts_cover_all_three_roles(session: Session) -> None:
    """证明播种脚本造出了管理员、编辑者与只读者三种账号。"""
    seed._ensure_accounts(  # noqa: SLF001
        session,
        admin_password="admin-password-123",
        user_password="user-password-123",
    )
    for username in ("admin", "editor", "viewer"):
        assert seed.get_user_by_username(session, username) is not None
    # 数据源只创建一次，不是每个账号一份。
    assert _count(session, DataSource) == 0


def test_init_demo_gate_sees_a_published_report(workspace) -> None:
    """证明初始化闸门能识别出已发布的主题，于是重跑不会凭空造东西。

    `init_demo.py` 原本次次都跑研究 → 生成 → 发布，每回都为同一窗口
    新造一次 Agent 运行和一份报告。这个闸门就是幂等守卫：
    已经走到 `published` 的主题没有什么可再演示的了。
    """
    import scripts.init_demo as init_demo

    session = workspace.session
    topic_id = 4242
    agent_run = workspace.add_agent_run(topic_id)
    workspace.add_report(
        topic_id=topic_id,
        agent_run_id=agent_run.id,
        status="published",
        title="Already published",
    )
    session.commit()

    assert init_demo._has_published_report(session, topic_id) is not None  # noqa: SLF001

    # 只有未发布报告的主题不进闸门：这一轮还有活要干。
    assert init_demo._has_published_report(session, topic_id + 1) is None  # noqa: SLF001
