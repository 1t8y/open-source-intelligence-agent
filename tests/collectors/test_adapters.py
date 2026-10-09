"""Tests for GitHub and Hugging Face collector adapters."""

import httpx
import pytest

from open_source_intelligence.collectors.github import GitHubCollector
from open_source_intelligence.collectors.http import RetryingHttpClient
from open_source_intelligence.collectors.huggingface import HuggingFaceCollector
from open_source_intelligence.collectors.types import SourceUnavailableError


def build_http(handler: httpx.MockTransport) -> RetryingHttpClient:
    """Build a retry client with no real delay for tests."""
    return RetryingHttpClient(
        httpx.Client(transport=handler),
        max_attempts=3,
        base_delay_seconds=0,
    )


def test_github_collector_normalizes_repository() -> None:
    """GitHub responses should map into the common repository shape."""

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
    """Hugging Face responses should map likes and tags consistently."""

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
    """Transient 5xx responses should be retried."""
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
    """Repeated server failures should raise a stable collector error."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    with pytest.raises(SourceUnavailableError):
        build_http(httpx.MockTransport(handler)).get_json("https://example.test/api")
