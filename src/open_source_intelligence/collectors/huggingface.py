"""通过 Hugging Face Hub API 采集模型。"""

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
    """通过 Hugging Face Hub API 采集模型。"""

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
        """从第一页采集并归一化至多 100 个 Hugging Face 模型。"""
        return list(self.collect_page(page=1, per_page=limit).items)

    def collect_page(self, *, page: int, per_page: int) -> CollectedPage:
        """返回从 1 开始计数的 Hugging Face 模型页，按下载量排序。

        走查基于偏移量，所以列表的尽头只能被「发现」，而不能被查到：API
        不公布总数，而 `skip` 越过末尾会返回一个空页而不是报错。因此一个
        不满的页是「没有更多」唯一诚实的信号，代价是每次走查结束时多一次
        确认请求。
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
        """返回该采集器分页遍历的搜索参数。"""
        return {"sort": "downloads", "direction": -1}

    @staticmethod
    def _normalize(item: dict[str, Any], observed_at: datetime) -> CollectedRepository:
        """归一化一个 Hugging Face 模型对象。"""
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
