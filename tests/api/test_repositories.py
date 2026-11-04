"""守护仓库目录与趋势端点：这条需求落地之后到底长什么样。

`REQUIREMENTS.md` FR-D-05 要求「报告、**仓库**、Agent、评测和审计 API」；
T18 补上了两条仓库路由和审计路由，于是这组用例把需求钉成可执行的定义：目录是
共享的公开元数据，趋势窗口把调用方点名的两天都算进去，而唯一带授权判断的那个
筛选器（`topic_id`）确实把判断执行到底。

写入只用一个 session 并提交，因为共享的内存数据库只把一条连接借给每个 session ——
留着未提交的写入事务会挡住应用自己的事务。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from api_support import Actor, auth_headers, build_client, create_actor
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace

from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.pipeline.queries import MAX_PAGE_SIZE

CATALOG = "/api/v1/repositories"


@dataclass(frozen=True, slots=True)
class Catalog:
    """夹具写入的目录，外加断言要用到的那些 id。"""

    open_topic_id: int
    closed_topic_id: int
    ranked_repository_id: int
    unranked_repository_id: int


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """管理员：连被停用的主题也读得到。"""
    return create_actor(session, username="admin", role="ADMIN")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """只读用户：目录是公开的，被停用的主题则不是。"""
    return create_actor(session, username="viewer", role="VIEWER")


@pytest.fixture()
def catalog(session: Session, workspace: Workspace) -> Catalog:
    """写入两个来源、三个仓库、两个主题，以及一小段快照序列。"""
    github = workspace.add_source(source_type="github")
    huggingface = workspace.add_source(source_type="huggingface")

    # 星数刻意不按字母序排列：这样即使排序被去掉，一个只会说「顺序看着对」的
    # 测试仍然会失败。
    ranked = workspace.add_repository(
        source_id=github.id,
        provider_repo_id="r-1",
        full_name="acme/rocket",
        stars=900,
        forks=90,
        language="Python",
        description="A tool-using agent runtime",
    )
    middle = workspace.add_repository(
        source_id=github.id,
        provider_repo_id="r-2",
        full_name="globex/compiler",
        stars=500,
        forks=500,
        language="Rust",
        description="A fast compiler",
    )
    smallest = workspace.add_repository(
        source_id=huggingface.id,
        provider_repo_id="r-3",
        full_name="initech/dataset",
        stars=100,
        forks=10,
        language="Jupyter Notebook",
        description=None,
    )

    open_topic = workspace.add_topic(name="Open topic", enabled=True)
    closed_topic = workspace.add_topic(name="Closed topic", enabled=False)
    workspace.link(open_topic.id, ranked.id, rank=1, score=Decimal("12.5"), selected=True)
    workspace.link(open_topic.id, middle.id, rank=2, score=Decimal("4.25"), selected=False)
    workspace.link(closed_topic.id, smallest.id, rank=1, score=Decimal("1"), selected=True)

    first_day = date(2026, 9, 20)
    for offset, stars in enumerate((880, 900, 900)):
        workspace.add_snapshot(
            ranked.id,
            first_day + timedelta(days=offset),
            stars=stars,
            forks=90,
            open_issues=3,
            stars_delta=None if offset == 0 else 20,
            forks_delta=None if offset == 0 else 0,
        )
    session.commit()
    return Catalog(
        open_topic_id=open_topic.id,
        closed_topic_id=closed_topic.id,
        ranked_repository_id=ranked.id,
        unranked_repository_id=smallest.id,
    )


def test_catalog_requires_a_session(client: TestClient) -> None:
    """目录里全是公开元数据，但它仍然要求登录。"""
    assert client.get(CATALOG).status_code == 401


def test_catalog_returns_the_provider_and_public_metadata(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """`provider` 来自来源表那一行，而不是来自仓库本身。"""
    response = client.get(CATALOG, headers=auth_headers(client, viewer))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    by_name = {item["full_name"]: item for item in body["items"]}
    assert by_name["acme/rocket"]["provider"] == "github"
    assert by_name["initech/dataset"]["provider"] == "huggingface"
    assert by_name["acme/rocket"]["license"] == "MIT"
    assert by_name["acme/rocket"]["topics"] == ["ai", "llm"]


def test_ranking_is_absent_unless_the_list_is_sliced_by_topic(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """分数、名次和入选标记是某次主题排序的产物，所以不带主题时它们是空值。

    换成返回 `0` 或 `false`，就等于宣称跑过一次从未发生的排序。
    """
    response = client.get(CATALOG, headers=auth_headers(client, viewer))
    assert response.status_code == 200
    for item in response.json()["items"]:
        assert item["score"] is None
        assert item["rank"] is None
        assert item["selected_for_agent"] is None


def test_catalog_is_sorted_by_stars_and_pages_stably(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """按星数倒序，并用 id 作为决胜键，让相邻两页不会重叠。"""
    headers = auth_headers(client, viewer)
    first = client.get(CATALOG, headers=headers, params={"page_size": 2}).json()
    second = client.get(CATALOG, headers=headers, params={"page_size": 2, "page": 2}).json()
    assert [item["full_name"] for item in first["items"]] == ["acme/rocket", "globex/compiler"]
    assert [item["full_name"] for item in second["items"]] == ["initech/dataset"]
    assert first["total"] == second["total"] == 3


def test_unknown_sort_is_refused_rather_than_defaulted(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """调用方要求了某种排序却没得到，它自己没有任何办法察觉。"""
    response = client.get(
        CATALOG,
        headers=auth_headers(client, viewer),
        params={"sort": "stars_delta"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_page_size_is_bounded(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """平台统一的 1–100 页长上限在这里同样生效。"""
    over = client.get(
        CATALOG,
        headers=auth_headers(client, viewer),
        params={"page_size": MAX_PAGE_SIZE + 1},
    )
    assert over.status_code == 422


def test_catalog_filters_narrow_the_total_as_well_as_the_page(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """`total` 必须服从同一组筛选，否则调用方会一页页翻进空白。"""
    headers = auth_headers(client, viewer)
    by_language = client.get(CATALOG, headers=headers, params={"language": "rust"}).json()
    assert by_language["total"] == 1
    assert by_language["items"][0]["full_name"] == "globex/compiler"

    by_stars = client.get(CATALOG, headers=headers, params={"min_stars": 400}).json()
    assert {item["full_name"] for item in by_stars["items"]} == {
        "acme/rocket",
        "globex/compiler",
    }
    assert by_stars["total"] == 2

    by_query = client.get(CATALOG, headers=headers, params={"q": "COMPILER"}).json()
    assert [item["full_name"] for item in by_query["items"]] == ["globex/compiler"]

    # `description` 可以为空，所以子串搜索不能把「描述为空」的行从每一次查询
    # 里都剔掉 —— 只能从确实不匹配的那些查询里剔掉。
    null_description = client.get(CATALOG, headers=headers, params={"q": "dataset"}).json()
    assert [item["full_name"] for item in null_description["items"]] == ["initech/dataset"]

    future = (datetime(2026, 10, 12, 12, 0, tzinfo=UTC) + timedelta(days=1)).isoformat()
    after = client.get(CATALOG, headers=headers, params={"created_after": future}).json()
    assert after["total"] == 0


def test_a_text_filter_is_a_literal_substring_rather_than_a_like_pattern(
    client: TestClient,
    session: Session,
    viewer: Actor,
    catalog: Catalog,
    workspace: Workspace,
) -> None:
    """`q` 是子串搜索，所以 `%` 与 `_` 是普通字符，不是 `LIKE` 通配符。

    直接塞进 `LIKE` 时，`q=_` 会变成 `LIKE '%_%'`，匹配所有至少含一个字符的行，
    而 `q=%` 会匹配整个目录 —— 调用方明明在搜一个字面字符，得到的却正好相反。
    第三种情况是另一半：名字里真的含这个字符时仍然必须找得到，否则「全部转义、
    让什么都匹配不上」也能让这个用例通过。
    """
    gitlab = workspace.add_source(source_type="gitlab")
    workspace.add_repository(
        source_id=gitlab.id,
        provider_repo_id="r-4",
        full_name="octo/under_score",
        stars=10,
        forks=1,
        language="Go",
        description="a name that really contains an underscore",
    )
    session.commit()

    headers = auth_headers(client, viewer)
    literal = client.get(CATALOG, headers=headers, params={"q": "_"}).json()
    assert [item["full_name"] for item in literal["items"]] == ["octo/under_score"]
    assert literal["total"] == 1

    wildcard = client.get(CATALOG, headers=headers, params={"q": "%"}).json()
    assert wildcard["total"] == 0
    assert wildcard["items"] == []


def test_a_timestamp_without_an_offset_is_refused_at_the_boundary(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """朴素时间戳会得到指出修法的 `422`，而不是查询内部抛出的 `500`。

    `UTCDateTime` 拒绝猜测时区，这是对的；但没有共享参数类型时，这种拒绝会以内
    部错误的形式到达调用方，看起来就像一次故障。
    """
    response = client.get(
        CATALOG,
        headers=auth_headers(client, viewer),
        params={"created_after": "2026-10-13T12:00:00"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert "timezone offset" in response.text


def test_topic_slice_carries_the_ranking(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """按主题切片时，返回该主题的候选，并带上它们的排序结果。"""
    response = client.get(
        CATALOG,
        headers=auth_headers(client, viewer),
        params={"topic_id": catalog.open_topic_id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    ranked = next(item for item in body["items"] if item["full_name"] == "acme/rocket")
    assert ranked["rank"] == 1
    assert ranked["score"] == "12.500000"
    assert ranked["selected_for_agent"] is True
    unselected = next(item for item in body["items"] if item["full_name"] == "globex/compiler")
    assert unselected["selected_for_agent"] is False


def test_a_disabled_topic_is_refused_and_audited(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
    session: Session,
) -> None:
    """目录里需要裁定的是「成员关系」，而拒绝要被记录下来。

    `403` 的响应体对那个主题只字不提，审计行里则点了它的名。这与报告 ACL 那面
    镜子一样：调用方一无所获，运维者仍然查得下去。
    """
    response = client.get(
        CATALOG,
        headers=auth_headers(client, viewer),
        params={"topic_id": catalog.closed_topic_id},
    )
    assert response.status_code == 403
    assert "Closed topic" not in response.text
    assert "topic" in response.json()["detail"]

    denied = session.scalars(
        select(AuditLog).where(
            AuditLog.action == "repository.read",
            AuditLog.result == "denied",
        )
    ).all()
    assert [row.resource_id for row in denied] == [str(catalog.closed_topic_id)]
    assert [row.actor_id for row in denied] == [viewer.user_id]
    assert denied[0].resource_type == "topic"


def test_an_administrator_may_slice_by_a_disabled_topic(
    client: TestClient,
    admin: Actor,
    catalog: Catalog,
) -> None:
    """`ADMIN` 读得到每一个主题，规则就这一条。"""
    response = client.get(
        CATALOG,
        headers=auth_headers(client, admin),
        params={"topic_id": catalog.closed_topic_id},
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


def test_missing_topic_is_a_not_found(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """不存在的主题是 `404`，与「看不见的主题」那个 `403` 分开。"""
    response = client.get(
        CATALOG,
        headers=auth_headers(client, viewer),
        params={"topic_id": catalog.closed_topic_id + 999},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_snapshot_window_includes_both_named_days(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """闭区间窗口是这条路由唯一与众不同的地方，所以在这里钉死。"""
    headers = auth_headers(client, viewer)
    response = client.get(
        f"{CATALOG}/{catalog.ranked_repository_id}/snapshots",
        headers=headers,
        params={"start": "2026-09-20", "end": "2026-09-22"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "github"
    assert body["granularity"] == "day"
    assert [item["snapshot_date"] for item in body["items"]] == [
        "2026-09-20",
        "2026-09-21",
        "2026-09-22",
    ]
    # 第一天没有前一天可比，所以它的增量是缺席，而不是零。
    assert body["items"][0]["stars_delta"] is None
    assert body["items"][1]["stars_delta"] == 20


def test_snapshot_window_edge_days_are_included(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """只点一天就返回那一天；换成半开区间则会返回空。"""
    response = client.get(
        f"{CATALOG}/{catalog.ranked_repository_id}/snapshots",
        headers=auth_headers(client, viewer),
        params={"start": "2026-09-22", "end": "2026-09-22"},
    )
    assert response.status_code == 200
    assert [item["snapshot_date"] for item in response.json()["items"]] == ["2026-09-22"]


def test_empty_snapshot_window_is_an_answer_not_an_error(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """区间内没有快照，也仍然是一个仓库，答案就是空列表。"""
    response = client.get(
        f"{CATALOG}/{catalog.ranked_repository_id}/snapshots",
        headers=auth_headers(client, viewer),
        params={"start": "2026-01-01", "end": "2026-01-05"},
    )
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_snapshot_window_bounds_are_refused_not_clamped(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """被悄悄夹到合法范围的窗口，等于在回答另一个时间段。"""
    headers = auth_headers(client, viewer)
    path = f"{CATALOG}/{catalog.ranked_repository_id}/snapshots"
    inverted = client.get(
        path,
        headers=headers,
        params={"start": "2026-09-22", "end": "2026-09-20"},
    )
    assert inverted.status_code == 422

    too_wide = client.get(
        path,
        headers=headers,
        params={"start": "2020-01-01", "end": "2026-09-20"},
    )
    assert too_wide.status_code == 422


def test_unknown_granularity_is_refused(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """目前只实现了 `day`，所以也只接受 `day`。"""
    response = client.get(
        f"{CATALOG}/{catalog.ranked_repository_id}/snapshots",
        headers=auth_headers(client, viewer),
        params={"granularity": "hour"},
    )
    assert response.status_code == 422


def test_unknown_repository_is_a_not_found(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """这里的资源就是仓库本身，所以它不存在时给出的那个码就是 `404`。"""
    response = client.get(
        f"{CATALOG}/{catalog.ranked_repository_id + 999}/snapshots",
        headers=auth_headers(client, viewer),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
