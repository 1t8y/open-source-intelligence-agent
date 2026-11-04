"""针对真实数据库跑 T18 的文档同步冒烟。

T18 的验收标准是「架构、API、数据模型和评测文档与代码一致」，而它之所以配得上这个
名字，是因为它翻出了契约声明过、代码却从未实现的四组 API 资源。补上那道缺口是任务
的一半；本脚本是另一半的证据 —— 它保留下来的两组（`FR-D-05` 点名的报告、仓库、
Agent、评测和审计 API）现在确实能用：

1. **仓库目录** —— 列表与趋势序列在真实行上作答；不点名主题时，排名字段说的是
   「从未排名」而不是零；停用的主题对只读角色拒绝、对管理员放行；趋势窗口两端都
   闭合。
2. **审计轨迹** —— 非管理员被拒，这次拒绝本身也进了账本，轨迹按从新到旧返回，
   `metadata_json` 不出进程。
3. **边界修复** —— 不带时区偏移的时间戳，在边界上是 `422`，而不是从查询构造器内部
   冒出来的 `500`。这个缺陷从 T12 起就潜伏在 `GET /reports` 里，只是因为 T18 拿
   文档对着代码读，才被发现。
4. **漂移检查器** —— 十项文档比较在进程内跑，于是同一条命令既产出运行时证据，也
   产出文档层面的结论。

这里没有任何一处重新实现被测代码。API 是进程内真正的 ASGI 应用，行由真正的安全
服务写入，目录经已发布的 `pipeline.queries` 层读取。
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from open_source_intelligence.api.app import create_app
from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.identity import Role, User, UserRole
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    RepoSnapshot,
    Topic,
    TopicRepository,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.security.audit import record_audit_event
from open_source_intelligence.security.passwords import hash_password
from open_source_intelligence.security.service import create_user

# --------------------------------------------------------------------------------------
# 演练自己持有的名字、日期与哨兵值
# --------------------------------------------------------------------------------------

#: 本演练创建的每一行都带这些前缀之一，清理那一趟才能按名字找到自己的活，而不是按
#: 某个 id 水位线。按名字清理正是演练能反复跑的原因：上一轮中断留下的行会被下一轮
#: 删掉，而不是被算进总数。
NAME_PREFIX = "__t18_drill__"
ACTOR_PREFIX = "__t18_"
#: 演练每个请求都带的 `X-Request-ID`。中间件把请求头抄进
#: `request.state.request_id`，`audit_logs.request_id` 又由它写入，因此正是它让演练的
#: *匿名*审计行变得可识别、可删除 —— 一次 401 会写下一行 `actor_id` 为 NULL 的记录，
#: 任何按 actor 的清理都够不着。没有它，本脚本每跑一次，就会在只追加的账本里永久
#: 留下一行 `auth.token` 失败。
REQUEST_ID_PREFIX = "__t18_drill__"
SOURCE_NAME = "__t18_drill_source__"
HF_SOURCE_NAME = "__t18_drill_hf_source__"
OPEN_TOPIC_NAME = "__T18 Open Drill Topic__"
CLOSED_TOPIC_NAME = "__T18 Closed Drill Topic__"
SMOKE_PASSWORD = "t18-smoke-password-not-for-production"

#: 埋在 `audit_logs.metadata_json` 里的哨兵值。只要这个字符串出现在任何响应里，就说明
#: 那个自由格式的列被接到了出口路径上，而没有人决定过它能带什么出去。
METADATA_SENTINEL = "__T18_METADATA_SENTINEL__"
IPV6_ADDRESS = "2001:db8::1"
IPV6_ACTION = "__t18_drill_ipv6__"
NO_IPV6_ACTION = "__t18_drill_no_ip__"
METADATA_ACTION = "__t18_drill_metadata__"

ROLES = {"admin": "ADMIN", "viewer": "VIEWER"}

#: star 数刻意不按仓库名的字母序排列：`alpha` / `beta` / `gamma` 是一个排法，
#: 100 / 160 / 130 是另一个，这样一旦 `ORDER BY` 被弄丢，关于返回顺序的断言也不会
#: 碰巧通过。
CATALOG = (
    {"slug": "alpha", "stars": 100, "forks": 4, "language": "Go"},
    {"slug": "beta", "stars": 160, "forks": 9, "language": "Python"},
    {"slug": "gamma", "stars": 130, "forks": 6, "language": "Python"},
)
STARS_DESCENDING = ("__t18_drill__/beta", "__t18_drill__/gamma", "__t18_drill__/alpha")

#: 三个快照，从旧到新。第一天的增量是 `None`，因为没有更早的观察可以相减 —— 已发布的
#: 快照阶段在那里就写 `None`，而写零等于断言计数没有动过。
SNAPSHOT_SHAPE = ((100, None), (130, 30), (160, 30))

DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 366


# --------------------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------------------


def purge_drill_rows(session: Session) -> None:
    """按依赖从旧到新的顺序，删掉本演练拥有的每一行。

    按名字而不是按 id 水位线删除，意味着即使上一次运行被中断、把库弄脏了，这一趟
    也站得住脚；也意味着连跑两次冒烟与只跑一次等价。

    名字是按**字面前缀**匹配的（`startswith(..., autoescape=True)`），而不是当作
    `LIKE` 模式。像 `__t18_drill__` 这样的前缀里有四个下划线，而 `_` 在 `LIKE` 里是
    单字符通配符，于是模式 `__t18_drill__%` 并不表示「以 `__t18_drill__` 开头」——
    它表示「以任意两个字符开头，然后是 `t18`，然后是任意一个字符，然后是 `drill`，
    然后是任意两个字符」—— 因此也会匹配本演练从未写过的值。actor 前缀是最要命的
    那个，因为清理正是按它匹配账号：`__t18_%` 能匹配用户名 `abt18c_smith`。转义
    之后，前缀才真的是前缀。本演练在这一点上并不特殊：每个冒烟都用含 `_` 的前缀
    给行打标。

    先把 id 读进 Python，而不是作为子查询传下去。MySQL 直接拒绝
    `DELETE FROM t WHERE id IN (SELECT ... FROM t)`（`1093 You can't specify
    target table for update in FROM clause`），所以一个看起来可移植的关联删除其实
    并不可移植：子查询必须在删除发出之前求值，而实体化做的正是这件事。
    """
    drill_user_ids = [
        int(value)
        for value in session.scalars(
            select(User.id).where(User.username.startswith(ACTOR_PREFIX, autoescape=True))
        )
    ]
    drill_topic_ids = [
        int(value)
        for value in session.scalars(
            select(Topic.id).where(Topic.name.in_([OPEN_TOPIC_NAME, CLOSED_TOPIC_NAME]))
        )
    ]
    drill_repository_ids = [
        int(value)
        for value in session.scalars(
            select(Repository.id).where(
                Repository.full_name.startswith(NAME_PREFIX, autoescape=True)
            )
        )
    ]

    # 审计清理的两半都不能少。按 actor 过滤能碰到演练账号直接造成的每一行；按请求 id
    # 过滤则是唯一能碰到「写给一个从未通过认证的调用方」的那些拒绝记录的途径 ——
    # 它们的 `actor_id` 按定义就是 NULL。
    session.execute(delete(AuditLog).where(AuditLog.actor_id.in_(drill_user_ids)))
    session.execute(
        delete(AuditLog).where(AuditLog.request_id.startswith(REQUEST_ID_PREFIX, autoescape=True))
    )
    session.execute(
        delete(RepoSnapshot).where(RepoSnapshot.repository_id.in_(drill_repository_ids))
    )
    session.execute(delete(TopicRepository).where(TopicRepository.topic_id.in_(drill_topic_ids)))
    session.execute(
        delete(TopicRepository).where(TopicRepository.repository_id.in_(drill_repository_ids))
    )
    session.execute(delete(Repository).where(Repository.id.in_(drill_repository_ids)))
    session.execute(delete(Topic).where(Topic.id.in_(drill_topic_ids)))
    session.execute(delete(DataSource).where(DataSource.name.in_([SOURCE_NAME, HF_SOURCE_NAME])))
    session.execute(delete(UserRole).where(UserRole.user_id.in_(drill_user_ids)))
    session.execute(delete(User).where(User.id.in_(drill_user_ids)))
    session.commit()


def count_drill_rows(session: Session) -> dict[str, int]:
    """按清理所用的同一批字面前缀，数一数演练还占着多少行。

    「把数据库还原成原样」这句承诺，就是这样从一句注释变成一次测量的：冒烟报告它留下
    了什么，验证脚本则断言那个计数为零。按前缀数，而不是按「比 X 更新的行」来数，
    才能在另一个进程也在写这份库时保持诚实。
    """
    drill_user_ids = [
        int(value)
        for value in session.scalars(
            select(User.id).where(User.username.startswith(ACTOR_PREFIX, autoescape=True))
        )
    ]
    return {
        "users": len(drill_user_ids),
        "repositories": int(
            session.scalar(
                select(func.count())
                .select_from(Repository)
                .where(Repository.full_name.startswith(NAME_PREFIX, autoescape=True))
            )
            or 0
        ),
        "topics": int(
            session.scalar(
                select(func.count())
                .select_from(Topic)
                .where(Topic.name.in_([OPEN_TOPIC_NAME, CLOSED_TOPIC_NAME]))
            )
            or 0
        ),
        # 数的是行数，不是一个标记：这里要防的残留恰恰是任何按 actor 的清理都看不到的
        # 那一行，所以只能按它自己的属性去数。
        "audit_rows": int(
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.request_id.startswith(REQUEST_ID_PREFIX, autoescape=True))
            )
            or 0
        ),
    }


def seed_actors(session: Session) -> dict[str, int]:
    """建出演练的两个账号，并按角色名返回它们的 id。"""
    for code, name in (("ADMIN", "Administrator"), ("VIEWER", "Viewer")):
        if session.scalar(select(Role.id).where(Role.code == code)) is None:
            session.add(Role(code=code, name=name))
    session.flush()

    ids: dict[str, int] = {}
    for suffix, role in ROLES.items():
        user = create_user(
            session,
            username=f"{ACTOR_PREFIX}{suffix}__",
            password_hash=hash_password(SMOKE_PASSWORD),
            display_name=f"T18 {suffix}",
            role_codes={role},
            data_scope="personal",
        )
        ids[suffix] = int(user.id)
    session.commit()
    return ids


def _ensure_source(session: Session, *, name: str, source_type: str, base_url: str) -> DataSource:
    """返回某个数据提供方的数据源；只有部署里一个都没有时才创建。

    `data_sources.source_type` 是唯一的，所以一个部署最多只有一个 GitHub 数据源，演练
    只能借用它，不能再加一个。借用本来也是更诚实的做法：目录暴露的 `provider` 列就
    来自这一行，复用应用自己创建的那一行，关于真实配置的断言才成立。
    """
    existing = session.scalar(
        select(DataSource).where(
            or_(DataSource.name == name, DataSource.source_type == source_type)
        )
    )
    if existing is not None:
        return existing
    source = DataSource(
        name=name,
        source_type=source_type,
        base_url=base_url,
        enabled=True,
        rate_limit_per_minute=30,
    )
    session.add(source)
    session.flush()
    return source


def seed_catalog(session: Session, *, actor_id: int) -> dict[str, Any]:
    """建出两个数据源、三个仓库、两个主题，以及三天的快照。

    要两个数据源，是因为 `provider` 列必须来自连接到的数据源行，而不是一个常量；把
    其中一个仓库放在第二个数据源上，那次连接才真的被走到。
    """
    github = _ensure_source(
        session,
        name=SOURCE_NAME,
        source_type="github",
        base_url="https://api.github.com",
    )
    huggingface = _ensure_source(
        session,
        name=HF_SOURCE_NAME,
        source_type="huggingface",
        base_url="https://hf-mirror.com/api",
    )

    now = utc_now()
    repository_ids: dict[str, int] = {}
    for index, spec in enumerate(CATALOG):
        # 一行挂在第二个数据源上，一行没有语言，一行的描述只有 `q` 过滤器够得着 ——
        # 每一行都是为了让某条过滤断言既有必须被排除的行，也有必须被找到的行。
        source = huggingface if spec["slug"] == "gamma" else github
        repository = Repository(
            source_id=source.id,
            provider_repo_id=f"{NAME_PREFIX}{spec['slug']}",
            full_name=f"{NAME_PREFIX}/{spec['slug']}",
            html_url=f"https://example.com/{NAME_PREFIX}/{spec['slug']}",
            description=(
                "T18 measurable drift sentinel" if spec["slug"] == "alpha" else "drill repository"
            ),
            language=None if spec["slug"] == "alpha" else str(spec["language"]),
            license="MIT",
            topics_json=["drill"],
            stars=int(spec["stars"]),
            forks=int(spec["forks"]),
            first_seen_at=now - timedelta(days=10 + index),
            last_seen_at=now,
        )
        session.add(repository)
        session.flush()
        repository_ids[spec["slug"]] = int(repository.id)

        today = now.date()
        for offset, (stars, delta) in enumerate(SNAPSHOT_SHAPE):
            session.add(
                RepoSnapshot(
                    repository_id=repository.id,
                    snapshot_date=today - timedelta(days=len(SNAPSHOT_SHAPE) - 1 - offset),
                    stars=stars + index,
                    forks=int(spec["forks"]),
                    open_issues=1,
                    stars_delta=delta,
                    forks_delta=delta,
                )
            )

    open_topic = Topic(
        name=OPEN_TOPIC_NAME,
        description="T18 smoke - visible to every role",
        keywords_json=[NAME_PREFIX],
        min_stars=0,
        window_days=7,
        max_candidates=3,
        publish_mode="MANUAL",
        enabled=True,
        created_by=actor_id,
    )
    closed_topic = Topic(
        name=CLOSED_TOPIC_NAME,
        description="T18 smoke - unpublished configuration",
        keywords_json=[NAME_PREFIX],
        min_stars=0,
        window_days=7,
        max_candidates=3,
        publish_mode="MANUAL",
        enabled=False,
        created_by=actor_id,
    )
    session.add_all([open_topic, closed_topic])
    session.flush()

    # 1 号排名给的是 star *最少*的那个，这样一段按 rank 排序的切片，就不会被误认成
    # 一段按 star 排序的切片。
    for slug, (score, rank) in (
        ("alpha", (Decimal("0.900000"), 1)),
        ("beta", (Decimal("0.500000"), 2)),
        ("gamma", (Decimal("0.100000"), 3)),
    ):
        for topic in (open_topic, closed_topic):
            session.add(
                TopicRepository(
                    topic_id=topic.id,
                    repository_id=repository_ids[slug],
                    score=score,
                    rank=rank,
                    selected_for_agent=rank <= 2,
                )
            )

    session.commit()
    return {
        "open_topic_id": int(open_topic.id),
        "closed_topic_id": int(closed_topic.id),
        "repository_ids": repository_ids,
    }


def seed_audit_rows(session: Session, *, actor_id: int) -> None:
    """把端点自己那几个字段要读的三条审计行铺好。"""
    record_audit_event(
        session,
        action=IPV6_ACTION,
        resource_type="drill",
        result="success",
        actor_id=actor_id,
        ip_address=IPV6_ADDRESS,
    )
    record_audit_event(
        session,
        action=NO_IPV6_ACTION,
        resource_type="drill",
        result="success",
        actor_id=actor_id,
    )
    record_audit_event(
        session,
        action=METADATA_ACTION,
        resource_type="drill",
        result="success",
        actor_id=actor_id,
        metadata_json={"note": METADATA_SENTINEL},
    )
    session.commit()


# --------------------------------------------------------------------------------------
# 辅助函数
# --------------------------------------------------------------------------------------


def _api_login(client: TestClient, suffix: str) -> dict[str, str]:
    """返回某个演练账号的 Bearer 请求头。"""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": f"{ACTOR_PREFIX}{suffix}__", "password": SMOKE_PASSWORD},
    )
    if response.status_code != 200:
        raise RuntimeError(f"API login for {suffix} failed: {response.status_code} {response.text}")
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _get(
    client: TestClient,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """发一次目录或审计请求，并返回响应。"""
    return client.get(path, headers=headers, params=params)


def _body(response: Any) -> dict[str, Any]:
    """返回成功响应的 JSON 体；不是成功响应，就带上状态码与正文抛错。"""
    if response.status_code != 200:
        raise RuntimeError(f"expected 200, got {response.status_code}: {response.text}")
    payload: dict[str, Any] = response.json()
    return payload


def _catalog(client: TestClient, headers: dict[str, str], **params: Any) -> dict[str, Any]:
    """读取演练在目录里的那一段。"""
    return _body(_get(client, "/api/v1/repositories", headers=headers, params=params))


# --------------------------------------------------------------------------------------
# 检查
# --------------------------------------------------------------------------------------


def catalog_checks(
    client: TestClient,
    viewer: dict[str, str],
    admin: dict[str, str],
    seeded: dict[str, Any],
) -> dict[str, Any]:
    """驱动仓库目录的各个端点，并报告它们答了什么。"""
    drill = {"q": NAME_PREFIX}

    anonymous = _get(client, "/api/v1/repositories", params=drill)
    listed = _catalog(client, viewer, **drill)
    page_one = _catalog(client, viewer, page=1, page_size=2, **drill)
    page_two = _catalog(client, viewer, page=2, page_size=2, **drill)
    ranked = _catalog(client, viewer, topic_id=seeded["open_topic_id"], **drill)

    closed = _get(
        client,
        "/api/v1/repositories",
        headers=viewer,
        params={"topic_id": seeded["closed_topic_id"], **drill},
    )
    closed_admin = _get(
        client,
        "/api/v1/repositories",
        headers=admin,
        params={"topic_id": seeded["closed_topic_id"], **drill},
    )

    alpha = next(item for item in listed["items"] if item["full_name"].endswith("/alpha"))
    gamma = next(item for item in listed["items"] if item["full_name"].endswith("/gamma"))

    return {
        "anonymous_status": anonymous.status_code,
        "total": listed["total"],
        "full_names_ordered": [item["full_name"] for item in listed["items"]],
        "providers": sorted({item["provider"] for item in listed["items"]}),
        "gamma_provider": gamma["provider"],
        "page_one_ids": [item["id"] for item in page_one["items"]],
        "page_two_ids": [item["id"] for item in page_two["items"]],
        "paging_overlap": len(
            {item["id"] for item in page_one["items"]} & {item["id"] for item in page_two["items"]}
        ),
        # 不点 `topic_id` 就意味着从未为这一行算过排名，这与「分数是零」或「没被选中」
        # 是两个不同的答案。
        "unranked_fields": [
            [item["score"], item["rank"], item["selected_for_agent"]] for item in listed["items"]
        ],
        "language_filter_total": _catalog(client, viewer, language="PYTHON", **drill)["total"],
        "min_stars_total": _catalog(client, viewer, min_stars=140, **drill)["total"],
        "description_query_total": _catalog(client, viewer, q="measurable drift")["total"],
        "future_created_after_total": _catalog(
            client,
            viewer,
            created_after="2099-01-01T00:00:00Z",
            **drill,
        )["total"],
        "unknown_sort_status": _get(
            client, "/api/v1/repositories", headers=viewer, params={"sort": "name", **drill}
        ).status_code,
        "page_size_cap_status": _get(
            client, "/api/v1/repositories", headers=viewer, params={"page_size": 101, **drill}
        ).status_code,
        # 从 T12 一直带到 T18 的边界缺陷：查询构造器会拒掉一个不带时区的时间戳，而
        # 这件事必须在边界上告诉调用方，而不是由一次 500 来说。
        "naive_timestamp_status": _get(
            client,
            "/api/v1/repositories",
            headers=viewer,
            params={"created_after": "2026-09-24T00:00:00", **drill},
        ).status_code,
        "naive_timestamp_detail": _get(
            client,
            "/api/v1/repositories",
            headers=viewer,
            params={"created_after": "2026-09-24T00:00:00", **drill},
        ).json()["detail"],
        "ranked_ranks": sorted(item["rank"] for item in ranked["items"]),
        "ranked_selected": [item["selected_for_agent"] for item in ranked["items"]],
        "ranked_score_of_alpha": next(
            item["score"] for item in ranked["items"] if item["full_name"].endswith("/alpha")
        ),
        "closed_topic_status": closed.status_code,
        "closed_topic_leaks_name": CLOSED_TOPIC_NAME in closed.text,
        "closed_topic_admin_status": closed_admin.status_code,
        "closed_topic_admin_total": _body(closed_admin)["total"],
        "missing_topic_status": _get(
            client, "/api/v1/repositories", headers=viewer, params={"topic_id": 999_999_999}
        ).status_code,
        "alpha_language": alpha["language"],
        "alpha_topics": alpha["topics"],
    }


def snapshot_checks(
    client: TestClient,
    viewer: dict[str, str],
    seeded: dict[str, Any],
) -> dict[str, Any]:
    """驱动仓库趋势端点，并报告它答了什么。"""
    repository_id = seeded["repository_ids"]["alpha"]
    path = f"/api/v1/repositories/{repository_id}/snapshots"
    today: date = utc_now().date()
    first = today - timedelta(days=len(SNAPSHOT_SHAPE) - 1)

    explicit = _body(
        _get(
            client,
            path,
            headers=viewer,
            params={"start": first.isoformat(), "end": today.isoformat()},
        )
    )
    defaulted = _body(_get(client, path, headers=viewer))
    # 状态码要在 `_body` 断言之前从响应上取下来，这样结果报的是端点实际答了什么，而不是
    # 把 `_body` 要求的那个 200 再复述一遍。一个写成常量的字段撑不起断言：只有在这里
    # 真的观察到 200，「空窗口是 200 而不是 404」才算是关于端点的主张。
    empty_response = _get(
        client,
        path,
        headers=viewer,
        params={
            "start": (today - timedelta(days=5)).isoformat(),
            "end": (today - timedelta(days=4)).isoformat(),
        },
    )
    empty_status = empty_response.status_code
    empty = _body(empty_response)
    single = _body(
        _get(
            client,
            path,
            headers=viewer,
            params={"start": today.isoformat(), "end": today.isoformat()},
        )
    )

    return {
        "closed_window_dates": [item["snapshot_date"] for item in explicit["items"]],
        "closed_window_resolved": [explicit["start_date"], explicit["end_date"]],
        # 本演练*请求*的窗口，与它实际拿到的窗口并排报出来。两者刚好相等，正是因为
        # 区间两端都闭合；而正是这个比较，让验证脚本不必硬写日期也能断言：在这里点名
        # 具体是哪几天，断言就只会在写演练的那一个日历日成立。
        "requested_window": [first.isoformat(), today.isoformat()],
        # 第一次观察没有可减的对象，所以增量是 `None` 而不是 `0`。这个区别与排名字段
        # 讲的是同一件事。
        "first_day_delta": explicit["items"][0]["stars_delta"],
        "second_day_delta": explicit["items"][1]["stars_delta"],
        "default_window_count": len(defaulted["items"]),
        "default_window_days": (
            date.fromisoformat(defaulted["end_date"]) - date.fromisoformat(defaulted["start_date"])
        ).days,
        "empty_window_status": empty_status,
        "empty_window_count": len(empty["items"]),
        "single_day_count": len(single["items"]),
        "single_day_date": single["items"][0]["snapshot_date"] if single["items"] else None,
        "inverted_window_status": _get(
            client,
            path,
            headers=viewer,
            params={"start": today.isoformat(), "end": first.isoformat()},
        ).status_code,
        "overlong_window_status": _get(
            client,
            path,
            headers=viewer,
            params={
                "start": (today - timedelta(days=MAX_WINDOW_DAYS + 10)).isoformat(),
                "end": today.isoformat(),
            },
        ).status_code,
        "unknown_granularity_status": _get(
            client, path, headers=viewer, params={"granularity": "week"}
        ).status_code,
        "missing_repository_status": _get(
            client, "/api/v1/repositories/999999999/snapshots", headers=viewer
        ).status_code,
        "anonymous_status": _get(client, path).status_code,
    }


def audit_checks(
    client: TestClient,
    viewer: dict[str, str],
    admin: dict[str, str],
    actor_ids: dict[str, int],
    factory: sessionmaker[Session],
) -> dict[str, Any]:
    """驱动审计端点，并报告它答了什么。"""
    refused = _get(client, "/api/v1/audit-logs", headers=viewer)
    # 观察得来的，不是复述：见 `empty_window_status` 那条说明。
    trail_response = _get(client, "/api/v1/audit-logs", headers=admin, params={"page_size": 100})
    admin_status = trail_response.status_code
    trail = _body(trail_response)
    returned_ids = [int(item["id"]) for item in trail["items"]]
    admin_trail = _body(
        _get(
            client,
            "/api/v1/audit-logs",
            headers=admin,
            params={"actor_id": actor_ids["admin"], "page_size": 100},
        )
    )

    ipv6 = _body(_get(client, "/api/v1/audit-logs", headers=admin, params={"action": IPV6_ACTION}))
    no_ipv6 = _body(
        _get(client, "/api/v1/audit-logs", headers=admin, params={"action": NO_IPV6_ACTION})
    )
    metadata_response = _get(
        client, "/api/v1/audit-logs", headers=admin, params={"action": METADATA_ACTION}
    )

    with factory() as session:
        refusal = session.scalars(
            select(AuditLog).where(
                AuditLog.actor_id == actor_ids["viewer"],
                AuditLog.action == "auth.rbac",
                AuditLog.result == "denied",
            )
        ).all()
        metadata_stored = session.scalar(
            select(AuditLog.metadata_json).where(AuditLog.action == METADATA_ACTION)
        )

    return {
        "anonymous_status": _get(client, "/api/v1/audit-logs").status_code,
        "viewer_status": refused.status_code,
        "viewer_refusals": len(refusal),
        "viewer_refusal_actor": refusal[0].actor_id if refusal else None,
        "viewer_refusal_resource_type": refusal[0].resource_type if refusal else None,
        "admin_status": admin_status,
        "newest_first": returned_ids == sorted(returned_ids, reverse=True),
        # 最新的一行是*只读角色*被拒那次，因为这次拒绝正是管理员读轨迹之前最后发生的
        # 事。`admin_total` 是更有力的主张：管理员自己那三行种子数据再加上它的登录，
        # 意味着端点读的是应用在写的账本，而不是为本演练专门搭的一个视图。
        "newest_action": trail["items"][0]["action"],
        "newest_actor": trail["items"][0]["actor_id"],
        "admin_total": admin_trail["total"],
        "admin_newest_action": admin_trail["items"][0]["action"],
        "ipv6_rendered": ipv6["items"][0]["ip_address"] if ipv6["items"] else None,
        "missing_ip_rendered": no_ipv6["items"][0]["ip_address"] if no_ipv6["items"] else "absent",
        # `metadata_json` 是存下来的 —— 正因如此这条断言才有意义 —— 而它绝不能通过
        # API 被读出来。
        "metadata_stored": metadata_stored is not None,
        "metadata_leaked": METADATA_SENTINEL in metadata_response.text
        or "metadata_json" in metadata_response.text,
        "unknown_result_status": _get(
            client, "/api/v1/audit-logs", headers=admin, params={"result": "maybe"}
        ).status_code,
        "page_size_cap_status": _get(
            client, "/api/v1/audit-logs", headers=admin, params={"page_size": 101}
        ).status_code,
        "naive_timestamp_status": _get(
            client,
            "/api/v1/audit-logs",
            headers=admin,
            params={"created_before": "2026-09-24T00:00:00"},
        ).status_code,
    }


def topic_denial_audit(factory: sessionmaker[Session], viewer_id: int) -> dict[str, Any]:
    """报告被拒的那段主题切片留下的审计行。

    从数据库读而不是走 API，因为这里要核对的调用方正是被拒的那一个：一次被拒者自己
    读得到的拒绝，算不上一道同样的控制。
    """
    with factory() as session:
        rows = session.scalars(
            select(AuditLog).where(
                AuditLog.actor_id == viewer_id,
                AuditLog.action == "repository.read",
                AuditLog.resource_type == "topic",
            )
        ).all()
    return {
        "count": len(rows),
        "resource_ids": sorted(row.resource_id or "" for row in rows),
        "results": sorted({row.result for row in rows}),
    }


def documentation_checks() -> dict[str, Any]:
    """在进程内跑那十项文档比较。

    导入而不是重写：检查器的要点就在于每次跑的都是同一个比较，在这里再来一份拷贝，
    就多了一个要跟着改的东西。
    """
    import check_docs_drift

    results = [check() for check in check_docs_drift.CHECKS]
    return {
        "passed": all(result.ok for result in results),
        # 拼成一个字符串而不是给列表，因为消费方是一条 PowerShell 断言，而
        # `@($null).Count` 是 1：一个空 JSON 数组和一个不存在的字段，过了
        # `ConvertFrom-Json` 就分不出来了，所以「没有漂移」必须能表达成标量。
        # `failures` 留给真正去读这个结果的人。
        "drifted": ", ".join(sorted(result.name for result in results if result.failures)),
        "failures": {result.name: result.failures for result in results if result.failures},
        "run": len(results),
    }


# --------------------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    """解析验证脚本使用的可选清理模式。"""
    parser = argparse.ArgumentParser(description="Run the T18 documentation-sync smoke")
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Only remove the drill's rows and exit",
    )
    return parser.parse_args()


def main() -> None:
    """对着配置好的数据库跑冒烟，并打印断言结果。"""
    args = _parse_args()
    settings: Settings = get_settings()
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)

    if args.cleanup_only:
        with factory() as session:
            purge_drill_rows(session)
        engine.dispose()
        print(json.dumps({"cleanup_only": True}, ensure_ascii=False, sort_keys=True))
        return

    payload: dict[str, Any] = {}
    try:
        with factory() as session:
            # 先清理再播种，否则上一轮被中断的运行会把某个 `total` 撑大，让演练因为
            # 错误的原因通过。
            purge_drill_rows(session)
            actor_ids = seed_actors(session)
            seeded = seed_catalog(session, actor_id=actor_ids["admin"])
            seed_audit_rows(session, actor_id=actor_ids["admin"])

        with TestClient(create_app(settings)) as client:
            # 每个请求都带一个演练自己的关联 id。中间件把它抄进
            # `request.state.request_id`，审计写入方把它存下来，清理又按它删除 —— 这是
            # 唯一能抓住「对一个未认证调用方的拒绝」所写下的那些行的把手，因为那些行不
            # 带任何 actor。HTTPX 会把这些客户端级请求头并进每个请求，所以逐次调用的
            # `Authorization` 头照旧生效。
            client.headers["X-Request-ID"] = f"{REQUEST_ID_PREFIX}{uuid4().hex[:8]}"
            admin = _api_login(client, "admin")
            viewer = _api_login(client, "viewer")
            payload["catalog"] = catalog_checks(client, viewer, admin, seeded)
            payload["snapshots"] = snapshot_checks(client, viewer, seeded)
            payload["audit"] = audit_checks(client, viewer, admin, actor_ids, factory)
            payload["catalog"]["closed_topic_audit"] = topic_denial_audit(
                factory, actor_ids["viewer"]
            )
        payload["documentation"] = documentation_checks()
        payload["expected"] = {
            "max_window_days": MAX_WINDOW_DAYS,
            "default_window_days": DEFAULT_WINDOW_DAYS,
            "name_prefix": NAME_PREFIX,
            "open_topic_id": seeded["open_topic_id"],
            "closed_topic_id": seeded["closed_topic_id"],
            "admin_id": actor_ids["admin"],
            "viewer_id": actor_ids["viewer"],
        }
    finally:
        with factory() as session:
            purge_drill_rows(session)
            # 清理之后才测量，这样结果回答的是「这一趟留下了什么」，而不是「它创建了
            # 什么」。干净通过时四项计数都是零；验证脚本断言的正是这一点，也正是它把
            # 那句「把数据库还原成原样」的承诺变成了一件可以失败的事。
            payload["leftovers"] = count_drill_rows(session)
        engine.dispose()

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
