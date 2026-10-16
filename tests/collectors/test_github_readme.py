"""Tests for the GitHub README fetch - the content half of the collector.

The distinction under test throughout is between *"the source answered"* and *"the source could
not answer"*: `None` versus `SourceUnavailableError`. It is the one thing the contents stage's
`missing` and `failed` counters are built on, so a fetch that collapsed the two would quietly
report a repository with no README as a broken upstream.
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
    """Build a retry client with no real delay for tests."""
    return RetryingHttpClient(
        httpx.Client(transport=handler),
        max_attempts=3,
        base_delay_seconds=0,
    )


def readme_payload(text: str, *, html_url: str = README_URL) -> dict[str, str]:
    """Build a GitHub-shaped README response carrying `text`."""
    return {
        "name": "README.md",
        "path": "README.md",
        "html_url": html_url,
        "encoding": "base64",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }


def test_a_readme_is_decoded_and_carries_an_https_source() -> None:
    """The fetched record should describe the document, not the wire format."""
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
    """GitHub's 404 means "there is none"; it must not be retried or raised."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    collector = GitHubCollector(build_http(httpx.MockTransport(handler)))

    assert collector.fetch_readme("example/repo") is None


def test_a_not_found_answer_is_not_retried() -> None:
    """A definite answer should cost exactly one request."""
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404)

    collector = GitHubCollector(build_http(httpx.MockTransport(handler)))
    collector.fetch_readme("example/repo")

    assert len(calls) == 1


def test_a_server_failure_is_retried_to_the_bound_and_then_raised() -> None:
    """A refusal is not an answer, so it keeps the retry semantics and surfaces as an error."""
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    collector = GitHubCollector(build_http(httpx.MockTransport(handler)))

    with pytest.raises(SourceUnavailableError):
        collector.fetch_readme("example/repo")
    assert len(calls) == 3


def test_a_response_that_is_not_an_object_is_a_source_failure() -> None:
    """A body of the wrong shape means we did not find out, not that there is no README."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    collector = GitHubCollector(build_http(httpx.MockTransport(handler)))

    with pytest.raises(SourceUnavailableError):
        collector.fetch_readme("example/repo")


def test_a_non_https_source_link_is_refused_before_it_reaches_the_cache() -> None:
    """Report citations carry this URL to readers, and only https passes the citation check."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=readme_payload("# x", html_url="http://example.com/r"))

    collector = GitHubCollector(build_http(httpx.MockTransport(handler)))

    with pytest.raises(SourceUnavailableError):
        collector.fetch_readme("example/repo")


def test_undecodable_bytes_are_replaced_rather_than_failing_the_readme() -> None:
    """Losing one character of an external document is not the same as losing the document."""
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
