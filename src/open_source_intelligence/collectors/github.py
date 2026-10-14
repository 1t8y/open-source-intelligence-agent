"""GitHub repository search collector."""

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

from open_source_intelligence.collectors.http import RetryingHttpClient
from open_source_intelligence.collectors.types import (
    CollectedPage,
    CollectedRepository,
    SourceUnavailableError,
    clamp_per_page,
    nonnegative_int,
    require_page,
)

LINK_PATTERN = re.compile(r"<([^>]+)>\s*;\s*rel\s*=\s*\"?([^\";,]+)\"?")
"""RFC 8288 `Link` header entries, as GitHub emits them."""


def parse_link_header(value: str | None) -> dict[str, str]:
    """Parse a `Link` header into a `rel -> target` map.

    This is the authoritative answer to "is there another page?". GitHub stops
    advertising `rel="next"` once a search walks past its 1000-result cap, so a page
    number derived from `total_count` would promise pages that the API will not serve.
    """
    if not value:
        return {}
    return {rel.strip(): url for url, rel in LINK_PATTERN.findall(value)}


class GitHubCollector:
    """Collect repositories through the GitHub Search API."""

    provider = "github"
    display_name = "GitHub"
    base_url = "https://api.github.com"

    def __init__(
        self,
        http: RetryingHttpClient,
        *,
        token: SecretStr | None = None,
        query: str = "topic:llm stars:>100",
        base_url: str | None = None,
    ) -> None:
        self._http = http
        self._query = query
        self.base_url = base_url or self.base_url
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "open-source-intelligence-agent",
        }
        if token is not None:
            self._headers["Authorization"] = f"Bearer {token.get_secret_value()}"

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        """Collect and normalize up to 100 GitHub repositories from the first page."""
        return list(self.collect_page(page=1, per_page=limit).items)

    def collect_page(self, *, page: int, per_page: int) -> CollectedPage:
        """Return one 1-based page of GitHub search results, ordered by stars."""
        require_page(page)
        clamp_per_page(per_page)
        response = self._http.get_json_response(
            f"{self.base_url}/search/repositories",
            params={
                "q": self._query,
                "sort": "stars",
                "order": "desc",
                "per_page": per_page,
                "page": page,
            },
            headers=self._headers,
        )
        payload = response.data
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise SourceUnavailableError("GitHub response did not contain an items list")
        observed_at = datetime.now(UTC)
        items = tuple(
            self._normalize(item, observed_at)
            for item in payload["items"]
            if isinstance(item, dict)
        )
        return CollectedPage(
            page=page,
            items=items,
            has_more="next" in parse_link_header(response.header("Link")),
        )

    def describe_query(self) -> dict[str, Any]:
        """Return the search parameters this collector pages."""
        return {"q": self._query, "sort": "stars", "order": "desc"}

    @staticmethod
    def _normalize(item: dict[str, Any], observed_at: datetime) -> CollectedRepository:
        """Normalize one GitHub repository object."""
        repository_id = item.get("id")
        full_name = item.get("full_name")
        html_url = item.get("html_url")
        if repository_id is None or not isinstance(full_name, str) or not isinstance(html_url, str):
            raise SourceUnavailableError("GitHub repository is missing required identity fields")
        license_data = item.get("license")
        license_name = license_data.get("spdx_id") if isinstance(license_data, dict) else None
        topics = item.get("topics")
        return CollectedRepository(
            provider="github",
            provider_repo_id=str(repository_id),
            full_name=full_name,
            html_url=html_url,
            description=item.get("description")
            if isinstance(item.get("description"), str)
            else None,
            language=item.get("language") if isinstance(item.get("language"), str) else None,
            license=license_name if isinstance(license_name, str) else None,
            topics=tuple(topic for topic in topics if isinstance(topic, str))
            if isinstance(topics, list)
            else (),
            stars=nonnegative_int(item.get("stargazers_count", 0), "stargazers_count"),
            forks=nonnegative_int(item.get("forks_count", 0), "forks_count"),
            observed_at=observed_at,
        )
