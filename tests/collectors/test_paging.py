"""Tests for page-by-page collection and the `Link` header it depends on."""

import httpx
import pytest

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
    """Build a retry client with no real delay."""
    return RetryingHttpClient(httpx.Client(transport=transport), base_delay_seconds=0)


def github_item(identifier: int) -> dict[str, object]:
    """Return one minimal GitHub repository object."""
    return {
        "id": identifier,
        "full_name": f"example/repo-{identifier}",
        "html_url": f"https://github.com/example/repo-{identifier}",
        "stargazers_count": 100,
        "forks_count": 5,
    }


def github_response(*, link: str | None, total_count: int, items: int = 2) -> httpx.Response:
    """Return a GitHub search response with the given paging header."""
    headers = {} if link is None else {"Link": link}
    return httpx.Response(
        200,
        json={"total_count": total_count, "items": [github_item(i) for i in range(items)]},
        headers=headers,
    )


# --------------------------------------------------------------------------- Link parsing


def test_the_link_header_maps_every_relation() -> None:
    """All four relations GitHub sends should be addressable by name."""
    assert parse_link_header(GITHUB_NEXT) == {
        "next": "https://api.github.com/search/repositories?q=llm&page=2",
        "last": "https://api.github.com/search/repositories?q=llm&page=500",
        "first": "https://api.github.com/search/repositories?q=llm&page=1",
    }


def test_a_missing_or_empty_link_header_is_an_empty_map() -> None:
    """No header and no entries both mean "no relations", not an error."""
    assert parse_link_header(None) == {}
    assert parse_link_header("") == {}


def test_a_header_without_a_next_relation_has_no_next() -> None:
    """The last page still advertises `prev` and `first`."""
    assert "next" not in parse_link_header(GITHUB_LAST_PAGE)


# ------------------------------------------------------------------------- GitHub paging


def test_github_pages_with_page_and_per_page() -> None:
    """A GitHub page is addressed by a 1-based number, not an offset."""
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


def test_github_reports_the_end_from_the_link_header_not_from_total_count() -> None:
    """A large `total_count` must not promise pages the search cap will refuse.

    GitHub stops advertising `rel="next"` once a query walks past its 1000-result cap while
    still reporting the full `total_count`. Deriving the last page from that number would
    invent pages the API answers with an empty body, so the walk would spend its whole rate
    limit budget collecting nothing.
    """
    collector = GitHubCollector(
        build_http(
            httpx.MockTransport(lambda _: github_response(link=GITHUB_LAST_PAGE, total_count=5252))
        )
    )

    page = collector.collect_page(page=9, per_page=50)

    assert page.has_more is False


def test_github_has_more_is_false_without_a_link_header() -> None:
    """An absent header is the same answer as an absent `next` relation."""
    collector = GitHubCollector(
        build_http(httpx.MockTransport(lambda _: github_response(link=None, total_count=3)))
    )

    assert collector.collect_page(page=1, per_page=10).has_more is False


def test_github_reads_a_header_case_insensitively() -> None:
    """A lowercase `link` header must page correctly.

    HTTP header names are case-insensitive but a plain dict is not. When the decoded response
    carried a `dict` of headers, a server sending `link` made every page look like the end of
    the history - a walk that silently collects one page and reports success.
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
    """A body without `items` is a source error, not an empty page."""
    from open_source_intelligence.collectors.types import SourceUnavailableError

    collector = GitHubCollector(
        build_http(httpx.MockTransport(lambda _: httpx.Response(200, json={"total_count": 1})))
    )

    with pytest.raises(SourceUnavailableError):
        collector.collect_page(page=1, per_page=10)


# -------------------------------------------------------------------- Hugging Face paging


def test_huggingface_pages_with_an_offset() -> None:
    """A Hugging Face page is addressed by `skip`, so page 3 of 4 starts at 8."""
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
    """`skip=0` is the honest way to say "from the start" on an offset API."""
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=[{"id": "org/model"}])

    HuggingFaceCollector(build_http(httpx.MockTransport(handler))).collect_page(page=1, per_page=4)

    assert seen[0].params["skip"] == "0"


def test_huggingface_treats_a_short_page_as_the_end() -> None:
    """The API publishes no total, so a short page is the only end-of-list signal."""
    collector = HuggingFaceCollector(
        build_http(
            httpx.MockTransport(lambda _: httpx.Response(200, json=[{"id": "a"}, {"id": "b"}]))
        )
    )

    assert collector.collect_page(page=5, per_page=3).has_more is False


def test_huggingface_treats_a_full_page_as_more_to_come() -> None:
    """A full page may or may not be the last one; the next request settles it."""
    collector = HuggingFaceCollector(
        build_http(
            httpx.MockTransport(
                lambda _: httpx.Response(200, json=[{"id": "a"}, {"id": "b"}, {"id": "c"}])
            )
        )
    )

    assert collector.collect_page(page=1, per_page=3).has_more is True


def test_huggingface_rejects_a_non_list_body() -> None:
    """A dict body is a source error, not an empty page."""
    from open_source_intelligence.collectors.types import SourceUnavailableError

    collector = HuggingFaceCollector(
        build_http(httpx.MockTransport(lambda _: httpx.Response(200, json={"error": "nope"})))
    )

    with pytest.raises(SourceUnavailableError):
        collector.collect_page(page=1, per_page=3)


# ------------------------------------------------------------------- one-page delegation


def test_collect_reads_the_first_page_of_each_provider() -> None:
    """The T4/T5 contract stays "give me N repositories", implemented by paging once."""
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


# --------------------------------------------------------------------------- bounds


@pytest.mark.parametrize(("page", "per_page"), [(0, 10), (-1, 10), (1, 0), (1, -5), (1, 101)])
def test_a_page_or_size_outside_the_bounds_is_refused(page: int, per_page: int) -> None:
    """Both providers refuse these, so the adapters refuse them before spending a request."""
    collector = GitHubCollector(
        build_http(httpx.MockTransport(lambda _: github_response(link=None, total_count=0)))
    )

    with pytest.raises(ValueError, match="must"):
        collector.collect_page(page=page, per_page=per_page)


def test_the_largest_accepted_page_is_the_shared_ceiling() -> None:
    """100 is what both providers honour, so it must not be refused."""
    collector = GitHubCollector(
        build_http(httpx.MockTransport(lambda _: github_response(link=None, total_count=0)))
    )

    assert collector.collect_page(page=1, per_page=MAX_PER_PAGE).page == 1


# ------------------------------------------------------------------- query provenance


def test_each_adapter_describes_the_search_it_pages() -> None:
    """A job records what was asked for, and only the adapter knows that."""
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
