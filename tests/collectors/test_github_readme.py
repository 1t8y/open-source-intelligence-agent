"""守护 GitHub README 拉取，也就是采集器的内容那一半。

贯穿全篇要区分的是「来源回答了」与「来源答不上来」：`None` 对
`SourceUnavailableError`。`contents` 阶段的 `missing` 与 `failed` 两个计数器
正是建在这条区分上，因此一旦把两者混为一谈，没有 README 的仓库就会被悄悄
报成上游故障。
"""

import base64

import httpx
import pytest

from open_source_intelligence.collectors.github import (
    GitHubCollector,
    decode_readme_content,
)
from open_source_intelligence.collectors.http import RetryingHttpClient
from open_source_intelligence.collectors.types import SourceUnavailableError

README_URL = "https://github.com/example/repo/blob/main/README.md"


def build_http(handler: httpx.MockTransport) -> RetryingHttpClient:
    """构造一个不做真实等待的重试客户端，用例才不会真的睡过去。"""
    return RetryingHttpClient(
        httpx.Client(transport=handler),
        max_attempts=3,
        base_delay_seconds=0,
    )


def readme_payload(text: str, *, html_url: str = README_URL) -> dict[str, str]:
    """构造一个携带 `text` 的、GitHub 形状的 README 响应。"""
    return {
        "name": "README.md",
        "path": "README.md",
        "html_url": html_url,
        "encoding": "base64",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }


def test_a_readme_is_decoded_and_carries_an_https_source() -> None:
    """拉取结果描述的是文档本身，而不是 base64 的传输格式。"""
    text = "# Rocket\n\nIt flies.\n"
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["accept"] = request.headers.get("accept", "")
        return httpx.Response(200, json=readme_payload(text))

    collector = GitHubCollector(build_http(httpx.MockTransport(handler)))
    fetched = collector.fetch_readme("example/repo")

    assert fetched is not None
    assert fetched.provider == "github"
    assert fetched.full_name == "example/repo"
    assert fetched.content_type == "readme"
    assert fetched.content == text
    assert fetched.source_url == README_URL
    assert fetched.fetched_at.tzinfo is not None
    assert seen["url"] == "https://api.github.com/repos/example/repo/readme"
    assert "github" in seen["accept"]


def test_a_repository_without_a_readme_is_an_answer_rather_than_a_failure() -> None:
    """GitHub 的 404 表示「本来就没有」，既不重试，也不抛错。"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    collector = GitHubCollector(build_http(httpx.MockTransport(handler)))

    assert collector.fetch_readme("example/repo") is None


def test_a_not_found_answer_is_not_retried() -> None:
    """确定的回答只应消耗一次请求。"""
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404)

    collector = GitHubCollector(build_http(httpx.MockTransport(handler)))
    collector.fetch_readme("example/repo")

    assert len(calls) == 1


def test_a_server_failure_is_retried_to_the_bound_and_then_raised() -> None:
    """拒绝回答不是回答：照旧走重试语义，最终以异常浮出。"""
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    collector = GitHubCollector(build_http(httpx.MockTransport(handler)))

    with pytest.raises(SourceUnavailableError):
        collector.fetch_readme("example/repo")
    assert len(calls) == 3


def test_a_response_that_is_not_an_object_is_a_source_failure() -> None:
    """响应体形状不对意味着「没问出来」，而不是「没有 README」。"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    collector = GitHubCollector(build_http(httpx.MockTransport(handler)))

    with pytest.raises(SourceUnavailableError):
        collector.fetch_readme("example/repo")


def test_a_non_https_source_link_is_refused_before_it_reaches_the_cache() -> None:
    """报告引用会把这个 URL 交给读者，而只有 https 能通过引用校验。"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=readme_payload("# x", html_url="http://example.com/r"))

    collector = GitHubCollector(build_http(httpx.MockTransport(handler)))

    with pytest.raises(SourceUnavailableError):
        collector.fetch_readme("example/repo")


def test_undecodable_bytes_are_replaced_rather_than_failing_the_readme() -> None:
    """外部文档里丢一个字符，不等于丢掉整份文档。"""
    payload = {
        "encoding": "base64",
        "content": base64.b64encode(b"\xff\xfe hello").decode("ascii"),
    }

    assert decode_readme_content(payload).endswith("hello")


def test_a_payload_with_no_content_is_a_source_failure() -> None:
    with pytest.raises(SourceUnavailableError):
        decode_readme_content({"encoding": "base64"})


def test_content_that_is_not_base64_is_a_source_failure() -> None:
    with pytest.raises(SourceUnavailableError):
        decode_readme_content({"encoding": "base64", "content": "a"})
