"""守护逐页采集，以及它依赖的 `Link` 响应头。"""

import httpx
import pytest
from support import build_settings

from open_source_intelligence.collectors.github import GitHubCollector, parse_link_header
from open_source_intelligence.collectors.http import RetryingHttpClient
from open_source_intelligence.collectors.huggingface import HuggingFaceCollector
from open_source_intelligence.collectors.types import MAX_PER_PAGE

GITHUB_NEXT = (
    '<https://api.github.com/search/repositories?q=llm&page=2>; rel="next", '
    '<https://api.github.com/search/repositories?q=llm&page=500>; rel="last", '
    '<https://api.github.com/search/repositories?q=llm&page=1>; rel="first"'
)
GITHUB_LAST_PAGE = (
    '<https://api.github.com/search/repositories?q=llm&page=9>; rel="prev", '
    '<https://api.github.com/search/repositories?q=llm&page=1>; rel="first"'
)


def build_http(transport: httpx.MockTransport) -> RetryingHttpClient:
    """构造一个不做真实等待的重试客户端。"""
    return RetryingHttpClient(httpx.Client(transport=transport), base_delay_seconds=0)


def github_item(identifier: int) -> dict[str, object]:
    """返回一个最简的 GitHub 仓库对象。"""
    return {
        "id": identifier,
        "full_name": f"example/repo-{identifier}",
        "html_url": f"https://github.com/example/repo-{identifier}",
        "stargazers_count": 100,
        "forks_count": 5,
        "open_issues_count": 12,
    }


def github_response(*, link: str | None, total_count: int, items: int = 2) -> httpx.Response:
    """返回一条带指定分页头的 GitHub 搜索响应。"""
    headers = {} if link is None else {"Link": link}
    return httpx.Response(
        200,
        json={"total_count": total_count, "items": [github_item(i) for i in range(items)]},
        headers=headers,
    )


# --------------------------------------------------------------------------- `Link` 头解析


def test_the_link_header_maps_every_relation() -> None:
    """GitHub 发送的每一种关系都应能按名字查到。"""
    assert parse_link_header(GITHUB_NEXT) == {
        "next": "https://api.github.com/search/repositories?q=llm&page=2",
        "last": "https://api.github.com/search/repositories?q=llm&page=500",
        "first": "https://api.github.com/search/repositories?q=llm&page=1",
    }


def test_a_missing_or_empty_link_header_is_an_empty_map() -> None:
    """没有响应头与没有条目都表示「没有关系」，而不是出错。"""
    assert parse_link_header(None) == {}
    assert parse_link_header("") == {}


def test_a_header_without_a_next_relation_has_no_next() -> None:
    """最后一页依旧会给出 `prev` 与 `first`。"""
    assert "next" not in parse_link_header(GITHUB_LAST_PAGE)


# ------------------------------------------------------------------------- GitHub 分页


def test_github_pages_with_page_and_per_page() -> None:
    """GitHub 的页用从 1 开始的页码寻址，而不是偏移量。"""
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return github_response(link=GITHUB_NEXT, total_count=5252)

    collector = GitHubCollector(build_http(httpx.MockTransport(handler)))
    page = collector.collect_page(page=3, per_page=25)

    assert seen[0].params["page"] == "3"
    assert seen[0].params["per_page"] == "25"
    assert page.page == 3
    assert page.has_more is True


def test_github_normalizes_the_open_issue_count() -> None:
    """GitHub 的 `open_issues_count` 必须流入归一化后的仓库。

    它此前被直接丢掉了，于是每份快照的 `open_issues` 都是空值，报告也只能承认
    存在数据覆盖缺口。
    """
    collector = GitHubCollector(
        build_http(httpx.MockTransport(lambda _request: github_response(link=None, total_count=1)))
    )
    page = collector.collect_page(page=1, per_page=1)

    assert page.items[0].open_issues == 12


def test_github_sort_is_configurable() -> None:
    """排序维度是构造参数，默认取 `stars`。

    每日流水线传 `updated`，以便发现近期活跃的仓库；默认值则让回填与一次性 CLI
    照旧按 star 数从高到低翻页。
    """
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return github_response(link=None, total_count=0)

    default = GitHubCollector(build_http(httpx.MockTransport(handler)))
    default.collect_page(page=1, per_page=10)
    assert seen[-1].params["sort"] == "stars"

    updated = GitHubCollector(build_http(httpx.MockTransport(handler)), sort="updated")
    updated.collect_page(page=1, per_page=10)
    assert seen[-1].params["sort"] == "updated"
    assert updated.describe_query()["sort"] == "updated"


def test_github_reports_the_end_from_the_link_header_not_from_total_count() -> None:
    """再大的 `total_count` 也不能许诺出搜索上限会拒绝的页。

    查询走过 1000 条结果的上限后，GitHub 就不再给出 `rel="next"`，
    但仍然上报完整的 `total_count`。若用它推算末页，就会凭空多出一些
    只返回空正文的页，遍历把整个速率预算花光却一无所获。
    """
    collector = GitHubCollector(
        build_http(
            httpx.MockTransport(lambda _: github_response(link=GITHUB_LAST_PAGE, total_count=5252))
        )
    )

    page = collector.collect_page(page=9, per_page=50)

    assert page.has_more is False


def test_github_has_more_is_false_without_a_link_header() -> None:
    """响应头缺失，与 `next` 关系缺失是同一个答案。"""
    collector = GitHubCollector(
        build_http(httpx.MockTransport(lambda _: github_response(link=None, total_count=3)))
    )

    assert collector.collect_page(page=1, per_page=10).has_more is False


def test_github_reads_a_header_case_insensitively() -> None:
    """小写的 `link` 响应头也必须能正确翻页。

    HTTP 头名不区分大小写，普通 dict 却区分。当解码后的响应带着一个装头的 `dict`
    时，服务端只要发 `link`，每一页都会被当成历史的终点 —— 遍历悄悄只采一页，还
    报告成功。
    """
    collector = GitHubCollector(
        build_http(
            httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={"total_count": 90, "items": [github_item(1)]},
                    headers={"link": GITHUB_NEXT},
                )
            )
        )
    )

    assert collector.collect_page(page=1, per_page=10).has_more is True


def test_github_rejects_a_missing_items_list() -> None:
    """没有 `items` 的响应体是来源错误，不是空页。"""
    from open_source_intelligence.collectors.types import SourceUnavailableError

    collector = GitHubCollector(
        build_http(httpx.MockTransport(lambda _: httpx.Response(200, json={"total_count": 1})))
    )

    with pytest.raises(SourceUnavailableError):
        collector.collect_page(page=1, per_page=10)


# -------------------------------------------------------------------- Hugging Face 分页


def test_huggingface_pages_with_an_offset() -> None:
    """Hugging Face 用 `skip` 寻址，所以每页 4 条时第 3 页从 8 开始。"""
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=[{"id": "org/model"}])

    collector = HuggingFaceCollector(build_http(httpx.MockTransport(handler)))
    page = collector.collect_page(page=3, per_page=4)

    assert seen[0].params["skip"] == "8"
    assert seen[0].params["limit"] == "4"
    assert page.page == 3


def test_huggingface_first_page_asks_for_no_offset() -> None:
    """在偏移量 API 上，`skip=0` 才是「从开头」的诚实写法。"""
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=[{"id": "org/model"}])

    HuggingFaceCollector(build_http(httpx.MockTransport(handler))).collect_page(page=1, per_page=4)

    assert seen[0].params["skip"] == "0"


def test_huggingface_treats_a_short_page_as_the_end() -> None:
    """该 API 不公布总数，因此不满一页就是唯一的列表结束信号。"""
    collector = HuggingFaceCollector(
        build_http(
            httpx.MockTransport(lambda _: httpx.Response(200, json=[{"id": "a"}, {"id": "b"}]))
        )
    )

    assert collector.collect_page(page=5, per_page=3).has_more is False


def test_huggingface_treats_a_full_page_as_more_to_come() -> None:
    """满页未必是最后一页，再请求一次才能定论。"""
    collector = HuggingFaceCollector(
        build_http(
            httpx.MockTransport(
                lambda _: httpx.Response(200, json=[{"id": "a"}, {"id": "b"}, {"id": "c"}])
            )
        )
    )

    assert collector.collect_page(page=1, per_page=3).has_more is True


def test_huggingface_rejects_a_non_list_body() -> None:
    """字典形状的响应体是来源错误，不是空页。"""
    from open_source_intelligence.collectors.types import SourceUnavailableError

    collector = HuggingFaceCollector(
        build_http(httpx.MockTransport(lambda _: httpx.Response(200, json={"error": "nope"})))
    )

    with pytest.raises(SourceUnavailableError):
        collector.collect_page(page=1, per_page=3)


# ------------------------------------------------------------------- 单页委托


def test_collect_reads_the_first_page_of_each_provider() -> None:
    """T4/T5 的契约仍是「给我 N 个仓库」，实现上只翻一页。"""
    github = GitHubCollector(
        build_http(
            httpx.MockTransport(
                lambda _: github_response(link=GITHUB_NEXT, total_count=100, items=4)
            )
        )
    )
    huggingface = HuggingFaceCollector(
        build_http(
            httpx.MockTransport(
                lambda _: httpx.Response(200, json=[{"id": f"org/m{index}"} for index in range(4)])
            )
        )
    )

    assert len(github.collect(limit=4)) == 4
    assert len(huggingface.collect(limit=4)) == 4


# --------------------------------------------------------------------------- 取值范围


@pytest.mark.parametrize(("page", "per_page"), [(0, 10), (-1, 10), (1, 0), (1, -5), (1, 101)])
def test_a_page_or_size_outside_the_bounds_is_refused(page: int, per_page: int) -> None:
    """两个来源都拒绝这些取值，所以适配器在花掉请求之前就先拒绝。"""
    collector = GitHubCollector(
        build_http(httpx.MockTransport(lambda _: github_response(link=None, total_count=0)))
    )

    with pytest.raises(ValueError, match="must"):
        collector.collect_page(page=page, per_page=per_page)


def test_the_largest_accepted_page_is_the_shared_ceiling() -> None:
    """100 是两个来源都接受的上限，因此不能拒。"""
    collector = GitHubCollector(
        build_http(httpx.MockTransport(lambda _: github_response(link=None, total_count=0)))
    )

    assert collector.collect_page(page=1, per_page=MAX_PER_PAGE).page == 1


# ------------------------------------------------------------------- 查询来源


def test_each_adapter_describes_the_search_it_pages() -> None:
    """任务要记录当时查了什么，而只有适配器知道这件事。"""
    github = GitHubCollector(
        build_http(httpx.MockTransport(lambda _: github_response(link=None, total_count=0))),
        query="topic:agent stars:>500",
    )
    huggingface = HuggingFaceCollector(
        build_http(httpx.MockTransport(lambda _: httpx.Response(200, json=[])))
    )

    assert github.describe_query() == {
        "q": "topic:agent stars:>500",
        "sort": "stars",
        "order": "desc",
    }
    assert huggingface.describe_query() == {"sort": "downloads", "direction": -1}


def test_build_collectors_forwards_the_github_sort() -> None:
    """`build_collectors` 必须把自己的 `github_sort` 传给 GitHub 适配器。

    每日流水线传 `"updated"`，以便发现近期活跃的仓库；默认值必须仍是
    `"stars"`，回填与一次性 CLI 才会继续按 star 数从高到低翻页。
    """
    from open_source_intelligence.collectors.cli import build_collectors

    with httpx.Client() as client:
        default = build_collectors("github", client, settings=build_settings())[0]
        assert default.describe_query()["sort"] == "stars"
        updated = build_collectors(
            "github", client, github_sort="updated", settings=build_settings()
        )[0]
        assert updated.describe_query()["sort"] == "updated"
