"""守护 GitHub 与 Hugging Face 采集适配器的归一化与重试行为。"""

import httpx
import pytest

from open_source_intelligence.collectors.github import GitHubCollector
from open_source_intelligence.collectors.http import RetryingHttpClient
from open_source_intelligence.collectors.huggingface import HuggingFaceCollector
from open_source_intelligence.collectors.types import SourceUnavailableError


def build_http(handler: httpx.MockTransport) -> RetryingHttpClient:
    """构造一个不做真实等待的重试客户端，用例才不会真的睡过去。"""
    return RetryingHttpClient(
        httpx.Client(transport=handler),
        max_attempts=3,
        base_delay_seconds=0,
    )


def test_github_collector_normalizes_repository() -> None:
    """GitHub 的响应要映射成统一的仓库结构，上层才不必按来源分支。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["per_page"] == "1"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": 42,
                        "full_name": "example/repo",
                        "html_url": "https://github.com/example/repo",
                        "description": "Agent runtime",
                        "language": "Python",
                        "license": {"spdx_id": "MIT"},
                        "topics": ["agent", "llm"],
                        "stargazers_count": 123,
                        "forks_count": 9,
                    }
                ]
            },
        )

    collector = GitHubCollector(build_http(httpx.MockTransport(handler)))
    items = collector.collect(limit=1)

    assert len(items) == 1
    assert items[0].provider == "github"
    assert items[0].provider_repo_id == "42"
    assert items[0].license == "MIT"
    assert items[0].topics == ("agent", "llm")


def test_huggingface_collector_normalizes_model() -> None:
    """Hugging Face 的响应要把点赞数与标签按同一套规则归一。"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": "example/model",
                    "likes": 88,
                    "tags": ["text-generation", "transformers"],
                    "pipeline_tag": "text-generation",
                    "cardData": {"license": "apache-2.0"},
                }
            ],
        )

    collector = HuggingFaceCollector(build_http(httpx.MockTransport(handler)))
    items = collector.collect(limit=1)

    assert len(items) == 1
    assert items[0].provider == "huggingface"
    assert items[0].provider_repo_id == "example/model"
    assert items[0].stars == 88
    assert items[0].forks == 0


def test_retrying_http_recovers_from_server_error() -> None:
    """偶发的 5xx 响应必须被重试，一次抖动不该让整轮采集落空。"""
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json={"ok": True})

    result = build_http(httpx.MockTransport(handler)).get_json("https://example.test/api")

    assert result == {"ok": True}
    assert attempts == 2


def test_retrying_http_exhausts_attempts() -> None:
    """重试次数用尽后应抛出稳定的采集错误，而不是原始 `httpx` 异常。"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    with pytest.raises(SourceUnavailableError):
        build_http(httpx.MockTransport(handler)).get_json("https://example.test/api")


def test_retrying_http_rejects_invalid_json() -> None:
    """HTTP 状态成功但 JSON 畸形时，必须记为来源错误，脏数据不得流进下游。"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    with pytest.raises(SourceUnavailableError):
        build_http(httpx.MockTransport(handler)).get_json("https://example.test/api")


def test_github_collector_rejects_invalid_metrics() -> None:
    """供应商给出的畸形计数不能以 `ValueError` 或 `TypeError` 逃出去。"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": 1,
                        "full_name": "example/repo",
                        "html_url": "https://github.com/example/repo",
                        "stargazers_count": "many",
                        "forks_count": 1,
                    }
                ]
            },
        )

    collector = GitHubCollector(build_http(httpx.MockTransport(handler)))
    with pytest.raises(SourceUnavailableError):
        collector.collect(limit=1)
