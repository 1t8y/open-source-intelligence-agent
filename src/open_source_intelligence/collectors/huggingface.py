"""Hugging Face model collector."""

from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

from open_source_intelligence.collectors.http import RetryingHttpClient
from open_source_intelligence.collectors.types import (
    CollectedRepository,
    SourceUnavailableError,
    nonnegative_int,
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
        """Collect and normalize up to 100 Hugging Face models."""
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        payload = self._http.get_json(
            f"{self.base_url}/models",
            params={
                "sort": "downloads",
                "direction": -1,
                "limit": limit,
                "full": "true",
            },
            headers=self._headers,
        )
        if not isinstance(payload, list):
            raise SourceUnavailableError("Hugging Face response was not a list")
        observed_at = datetime.now(UTC)
        return [self._normalize(item, observed_at) for item in payload if isinstance(item, dict)]

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
