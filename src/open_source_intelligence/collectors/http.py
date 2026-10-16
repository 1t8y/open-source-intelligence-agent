"""Retrying synchronous HTTP helper for public repository APIs."""

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from open_source_intelligence.collectors.types import SourceUnavailableError


@dataclass(frozen=True, slots=True)
class JsonResponse:
    """Decoded JSON together with the headers a caller may need in order to page.

    Pagination is answered in a header, not in a body: GitHub states whether another page
    exists in `Link`. A client that could only ever return `response.json()` would push
    every adapter into a second request path that re-implements retry and pacing by hand,
    which is how the retry policy ends up with two definitions that drift.

    `headers` keys are lowercased, because a plain dict is case-sensitive while HTTP header
    names are not. Reading a header through `header()` is therefore not a convenience but
    the only correct way: `headers["Link"]` would silently miss the `link` that the server
    actually sent, and a paging walk reading a missing `Link` concludes it has reached the
    end of a history it has not begun to read.
    """

    data: Any
    headers: Mapping[str, str]

    def header(self, name: str) -> str | None:
        """Return one header case-insensitively."""
        return self.headers.get(name.lower())


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
        return self.get_json_response(url, params=params, headers=headers).data

    def get_json_response(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        headers: dict[str, str] | None = None,
    ) -> JsonResponse:
        """Return decoded JSON and the response headers, retrying transient failures."""
        response = self._request_json(url, params=params, headers=headers, allow_not_found=False)
        if response is None:  # pragma: no cover - `allow_not_found=False` never returns None
            raise SourceUnavailableError(f"failed to collect from {url}: unexpected empty response")
        return response

    def get_json_or_none(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        headers: dict[str, str] | None = None,
    ) -> JsonResponse | None:
        """Return decoded JSON, or `None` when the source answers `404`.

        A `404` on a per-resource endpoint is the source stating a fact - this repository
        has no README - and it must stay distinguishable from "the request failed". Folding
        it into the retry path would turn a definite answer into a retried error and then
        into a reported failure, which is how a repository with no README ends up looking
        like a broken source. Every other status keeps the retry semantics of
        `get_json_response`.
        """
        return self._request_json(url, params=params, headers=headers, allow_not_found=True)

    def _request_json(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None,
        headers: dict[str, str] | None,
        allow_not_found: bool,
    ) -> JsonResponse | None:
        """Run the bounded retry loop and decode the body of a successful response."""
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                response = self._client.get(url, params=params, headers=headers)
                if response.status_code == 404 and allow_not_found:
                    return None
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
                    return JsonResponse(
                        data=response.json(),
                        headers={key.lower(): value for key, value in response.headers.items()},
                    )
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
