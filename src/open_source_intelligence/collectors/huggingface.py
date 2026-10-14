"""Hugging Face model collector."""

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


class HuggingFaceCollector:
    """Collect models through the Hugging Face Hub API."""

    provider = "huggingface"
    display_name = "Hugging Face"
    base_url = "https://huggingface.co/api"

    def __init__(
        self,
        http: RetryingHttpClient,
        *,
        token: SecretStr | None = None,
        base_url: str | None = None,
    ) -> None:
        self._http = http
        self.base_url = base_url or self.base_url
        self._headers = {"User-Agent": "open-source-intelligence-agent"}
        if token is not None:
            self._headers["Authorization"] = f"Bearer {token.get_secret_value()}"

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        """Collect and normalize up to 100 Hugging Face models from the first page."""
        return list(self.collect_page(page=1, per_page=limit).items)

    def collect_page(self, *, page: int, per_page: int) -> CollectedPage:
        """Return one 1-based page of Hugging Face models, ordered by downloads.

        The walk is offset-based, so the end of the list has to be discovered rather than
        looked up: the API publishes no total, and `skip` past the end answers with an
        empty page instead of an error. A short page is therefore the only honest signal
        that there is no more, which costs one confirming request at the end of a walk.
        """
        require_page(page)
        clamp_per_page(per_page)
        response = self._http.get_json_response(
            f"{self.base_url}/models",
            params={
                "sort": "downloads",
                "direction": -1,
                "limit": per_page,
                "skip": (page - 1) * per_page,
                "full": "true",
            },
            headers=self._headers,
        )
        payload = response.data
        if not isinstance(payload, list):
            raise SourceUnavailableError("Hugging Face response was not a list")
        observed_at = datetime.now(UTC)
        items = tuple(
            self._normalize(item, observed_at) for item in payload if isinstance(item, dict)
        )
        return CollectedPage(page=page, items=items, has_more=len(items) == per_page)

    def describe_query(self) -> dict[str, Any]:
        """Return the search parameters this collector pages."""
        return {"sort": "downloads", "direction": -1}

    @staticmethod
    def _normalize(item: dict[str, Any], observed_at: datetime) -> CollectedRepository:
        """Normalize one Hugging Face model object."""
        model_id = item.get("id") or item.get("modelId")
        if not isinstance(model_id, str):
            raise SourceUnavailableError("Hugging Face model is missing its id")
        tags = item.get("tags")
        card_data = item.get("cardData")
        license_name = card_data.get("license") if isinstance(card_data, dict) else None
        return CollectedRepository(
            provider="huggingface",
            provider_repo_id=model_id,
            full_name=model_id,
            html_url=f"https://huggingface.co/{model_id}",
            description=item.get("pipeline_tag")
            if isinstance(item.get("pipeline_tag"), str)
            else None,
            language=None,
            license=license_name if isinstance(license_name, str) else None,
            topics=tuple(tag for tag in tags if isinstance(tag, str))
            if isinstance(tags, list)
            else (),
            stars=nonnegative_int(item.get("likes", 0), "likes"),
            forks=0,
            observed_at=observed_at,
        )
