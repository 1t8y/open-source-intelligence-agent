"""GitHub repository search collector."""

from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

from open_source_intelligence.collectors.http import RetryingHttpClient
from open_source_intelligence.collectors.types import (
    CollectedRepository,
    SourceUnavailableError,
    nonnegative_int,
)


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
        """Collect and normalize up to 100 GitHub repositories."""
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        payload = self._http.get_json(
            f"{self.base_url}/search/repositories",
            params={
                "q": self._query,
                "sort": "stars",
                "order": "desc",
                "per_page": limit,
            },
            headers=self._headers,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise SourceUnavailableError("GitHub response did not contain an items list")
        observed_at = datetime.now(UTC)
        repositories: list[CollectedRepository] = []
        for item in payload["items"]:
            if isinstance(item, dict):
                repositories.append(self._normalize(item, observed_at))
        return repositories

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
