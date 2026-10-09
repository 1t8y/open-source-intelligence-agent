"""Retrying synchronous HTTP helper for public repository APIs."""

import time
from typing import Any

import httpx

from open_source_intelligence.collectors.types import SourceUnavailableError


class RetryingHttpClient:
    """Retry transient source failures with bounded exponential backoff."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        max_attempts: int = 3,
        base_delay_seconds: float = 0.5,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._client = client
        self._max_attempts = max_attempts
        self._base_delay_seconds = base_delay_seconds

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Return decoded JSON, retrying rate limits and server failures."""
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                response = self._client.get(url, params=params, headers=headers)
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = SourceUnavailableError(
                        f"source returned HTTP {response.status_code}"
                    )
                    if attempt + 1 < self._max_attempts:
                        self._sleep_before_retry(response, attempt)
                        continue
                    break
                response.raise_for_status()
                try:
                    return response.json()
                except ValueError as exc:
                    last_error = SourceUnavailableError("source returned invalid JSON")
                    raise last_error from exc
            except httpx.HTTPStatusError as exc:
                last_error = exc
                break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt + 1 < self._max_attempts:
                    time.sleep(self._base_delay_seconds * (2**attempt))
                    continue
                break
        message = f"failed to collect from {url}: {last_error or 'unknown error'}"
        raise SourceUnavailableError(message) from last_error

    def _sleep_before_retry(self, response: httpx.Response, attempt: int) -> None:
        """Honor Retry-After when present, otherwise use exponential backoff."""
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                time.sleep(max(float(retry_after), self._base_delay_seconds))
                return
            except ValueError:
                pass
        time.sleep(self._base_delay_seconds * (2**attempt))
