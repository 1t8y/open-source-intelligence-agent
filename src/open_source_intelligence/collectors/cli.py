"""本地运行与验证用的一次性采集命令。"""

import argparse
import json

import httpx

from open_source_intelligence.collectors.github import GitHubCollector
from open_source_intelligence.collectors.http import RetryingHttpClient
from open_source_intelligence.collectors.huggingface import HuggingFaceCollector
from open_source_intelligence.collectors.service import collect_repositories
from open_source_intelligence.collectors.types import RepositoryCollector, RepositoryContentSource
from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)


def build_collectors(
    provider: str,
    client: httpx.Client,
    *,
    github_sort: str = "stars",
    settings: Settings | None = None,
) -> list[RepositoryCollector]:
    """用配置好的可选令牌构建选中的采集器。

    `github_sort` 决定 GitHub 搜索结果如何排序。每日流水线传
    `"updated"`，以便发现近期活跃的仓库，而不是每天重复拉取同一批 star
    榜前列；回填与一次性 CLI 保留默认的 `"stars"`，这样它们的走查仍然
    从分布头部开始翻页。

    `settings` 可注入，是为了让测试能用 `Settings(_env_file=None)` 构
    建采集器，而不是去读开发者真实的 `.env`；生产调用方保持它为
    `None`。
    """
    settings = settings or get_settings()
    http = RetryingHttpClient(client)
    collectors: dict[str, RepositoryCollector] = {
        "github": GitHubCollector(
            http,
            token=settings.github_token,
            base_url=settings.github_api_base,
            sort=github_sort,
        ),
        "huggingface": HuggingFaceCollector(
            http,
            token=settings.huggingface_token,
            base_url=settings.huggingface_api_base,
        ),
    }
    selected = list(collectors.values()) if provider == "all" else [collectors[provider]]
    return selected


def build_content_sources(provider: str, client: httpx.Client) -> list[RepositoryContentSource]:
    """构建所选供应商中能提供内容的那些源。

    从 `build_collectors` 里筛出来，而不是另外构造，这样「哪些供应商能
    提供 README」由适配器自己回答：没有实现 `RepositoryContentSource`
    的供应商自然被筛掉，给某个供应商加上这项能力，也只是让它不再被跳
    过。在这里另建一个 GitHub 客户端，还会让采集器对象与内容源变成两个
    对象共用一份配置。
    """
    return [
        collector
        for collector in build_collectors(provider, client)
        if isinstance(collector, RepositoryContentSource)
    ]


def parse_args() -> argparse.Namespace:
    """解析供应商与数量参数。"""
    parser = argparse.ArgumentParser(description="Run one repository collection batch")
    parser.add_argument("--provider", choices=("github", "huggingface", "all"), default="all")
    parser.add_argument("--limit", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    """采集一批并打印 JSON 摘要。"""
    args = parse_args()
    if not 1 <= args.limit <= 100:
        raise SystemExit("--limit must be between 1 and 100")
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        with session_factory() as session:
            summary = collect_repositories(
                session,
                build_collectors(args.provider, client),
                limit=args.limit,
            )
    engine.dispose()
    print(
        json.dumps(
            {
                "provider_counts": summary.provider_counts,
                "errors": summary.errors,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
