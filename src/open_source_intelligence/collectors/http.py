"""为公共仓库 API 提供带重试的同步 HTTP 调用。"""

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from open_source_intelligence.collectors.types import SourceUnavailableError


@dataclass(frozen=True, slots=True)
class JsonResponse:
    """解码后的 JSON，以及调用方分页时可能需要的响应头。

    分页的答案在响应头里，而不在响应体里：GitHub 用 `Link` 说明是否还
    存在下一页。一个只能返回 `response.json()` 的客户端，会把每个适配
    器都逼进第二条请求路径，在那里手工重新实现重试与节流 —— 重试策略
    就是这么变成两份、然后逐渐漂移的。

    `headers` 的键统一转成小写，因为普通 dict 区分大小写，而 HTTP 头
    名不区分。所以通过 `header()` 读头不是便利，而是唯一正确的做法：
    `headers["Link"]` 会悄悄错过服务器实际发出的 `link`，而一个分页
    走查读到缺失的 `Link`，就会认定自己已经走到一段尚未开始读的历史的
    尽头。
    """

    data: Any
    headers: Mapping[str, str]

    def header(self, name: str) -> str | None:
        """按大小写不敏感的方式返回一个响应头。"""
        return self.headers.get(name.lower())


class RetryingHttpClient:
    """以有界指数退避重试源的瞬时故障。"""

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
        """返回解码后的 JSON，并对限流与服务器故障重试。"""
        return self.get_json_response(url, params=params, headers=headers).data

    def get_json_response(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        headers: dict[str, str] | None = None,
    ) -> JsonResponse:
        """返回解码后的 JSON 与响应头，对瞬时故障重试。"""
        response = self._request_json(url, params=params, headers=headers, allow_not_found=False)
        if response is None:  # pragma: no cover - `allow_not_found=False` 时不会返回 None
            raise SourceUnavailableError(f"failed to collect from {url}: unexpected empty response")
        return response

    def get_json_or_none(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        headers: dict[str, str] | None = None,
    ) -> JsonResponse | None:
        """返回解码后的 JSON；源回 `404` 时返回 `None`。

        在针对单个资源的端点上，`404` 是源在陈述一个事实 —— 这个仓库
        没有 README —— 它必须与「请求失败了」保持可区分。把它并进重试
        路径，会把一个确定的答案变成一次被重试的错误，再变成一次被上报
        的失败；一个没有 README 的仓库，就是这么看起来像坏掉的源。其它
        任何状态码都保留 `get_json_response` 的重试语义。
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
        """运行有界重试循环，并解码成功响应的响应体。"""
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
        """响应头里有 `Retry-After` 就照办，否则用指数退避。"""
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                time.sleep(max(float(retry_after), self._base_delay_seconds))
                return
            except ValueError:
                pass
        time.sleep(self._base_delay_seconds * (2**attempt))
