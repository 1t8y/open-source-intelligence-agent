"""通过 GitHub 搜索 API 采集仓库。"""

import base64
import binascii
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from pydantic import SecretStr

from open_source_intelligence.collectors.http import RetryingHttpClient
from open_source_intelligence.collectors.types import (
    CollectedPage,
    CollectedRepository,
    FetchedContent,
    SourceUnavailableError,
    clamp_per_page,
    nonnegative_int,
    require_page,
)

LINK_PATTERN = re.compile(r"<([^>]+)>\s*;\s*rel\s*=\s*\"?([^\";,]+)\"?")
"""RFC 8288 `Link` 头的条目格式，与 GitHub 输出的形态一致。"""


def parse_link_header(value: str | None) -> dict[str, str]:
    """解析 `Link` 头，得到 `rel -> 目标` 的映射。

    这才是「是否还有下一页」的权威答案。一旦搜索走过 1000 条上限，
    GitHub 就不再声明 `rel="next"`，所以用 `total_count` 推出来的页
    码，会承诺一些 API 根本不会提供的页。
    """
    if not value:
        return {}
    return {rel.strip(): url for url, rel in LINK_PATTERN.findall(value)}


def decode_readme_content(payload: dict[str, Any]) -> str:
    """解码 GitHub 返回的 README 正文，它默认是 base64。

    无法解码的字节被替换而不是被拒绝：README 是模型用来理解上下文的外部
    文本，丢一个怪字符与丢掉整份文档并不是同一种失败。而响应里完全没有
    内容*才*是那种失败，所以它抛异常。
    """
    raw = payload.get("content")
    if not isinstance(raw, str) or not raw:
        raise SourceUnavailableError("GitHub README response carried no content")
    if payload.get("encoding") != "base64":
        return raw
    try:
        decoded = base64.b64decode(raw)
    except (binascii.Error, ValueError) as exc:
        raise SourceUnavailableError("GitHub README content is not valid base64") from exc
    return decoded.decode("utf-8", errors="replace")


class GitHubCollector:
    """通过 GitHub 搜索 API 采集仓库，并抓取它们的 README。"""

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
        sort: str = "stars",
    ) -> None:
        self._http = http
        self._query = query
        self._sort = sort
        self.base_url = base_url or self.base_url
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "open-source-intelligence-agent",
        }
        if token is not None:
            self._headers["Authorization"] = f"Bearer {token.get_secret_value()}"

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        """从第一页采集并归一化至多 100 个 GitHub 仓库。"""
        return list(self.collect_page(page=1, per_page=limit).items)

    def collect_page(self, *, page: int, per_page: int) -> CollectedPage:
        """返回从 1 开始计数的 GitHub 搜索结果页，按配置的排序方式排列。"""
        require_page(page)
        clamp_per_page(per_page)
        response = self._http.get_json_response(
            f"{self.base_url}/search/repositories",
            params={
                "q": self._query,
                "sort": self._sort,
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
        """返回该采集器分页遍历的搜索参数。"""
        return {"q": self._query, "sort": self._sort, "order": "desc"}

    def fetch_readme(self, full_name: str) -> FetchedContent | None:
        """返回某个仓库的 README；GitHub 没有可提供的内容时返回 `None`。

        README 端点对「没有 README」和「没有这个仓库」都回 `404`，本方法
        刻意把两者都报成 `None`：从调用方的角度看它们是同一个事实 —— 这
        个名字没有可缓存的内容 —— 另一种做法是从两种情形都缺失的响应体里
        去猜到底是哪一种。仓库是从 `topic_repositories` 走到这里的，所以
        一个不存在的名字本就属于异常；把它变成一次被重试的源故障只会更
        糟，因为那会让一行过期数据看起来像上游坏了。
        """
        response = self._http.get_json_or_none(
            f"{self.base_url}/repos/{quote(full_name, safe='/')}/readme",
            headers=self._headers,
        )
        if response is None:
            return None
        payload = response.data
        if not isinstance(payload, dict):
            raise SourceUnavailableError("GitHub README response was not an object")
        html_url = payload.get("html_url")
        if not isinstance(html_url, str) or not html_url.startswith("https://"):
            raise SourceUnavailableError("GitHub README response is missing an https html_url")
        return FetchedContent(
            provider=self.provider,
            full_name=full_name,
            content_type="readme",
            source_url=html_url,
            content=decode_readme_content(payload),
            fetched_at=datetime.now(UTC),
        )

    @staticmethod
    def _normalize(item: dict[str, Any], observed_at: datetime) -> CollectedRepository:
        """归一化一个 GitHub 仓库对象。"""
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
            open_issues=nonnegative_int(item.get("open_issues_count", 0), "open_issues_count"),
            observed_at=observed_at,
        )
