"""One-shot collector command for local operation and verification."""

import argparse
import json

import httpx

from open_source_intelligence.collectors.github import GitHubCollector
from open_source_intelligence.collectors.http import RetryingHttpClient
from open_source_intelligence.collectors.huggingface import HuggingFaceCollector
from open_source_intelligence.collectors.service import collect_repositories
from open_source_intelligence.collectors.types import RepositoryCollector
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)


def build_collectors(provider: str, client: httpx.Client) -> list[RepositoryCollector]:
    """Build selected collectors using configured optional tokens."""
    settings = get_settings()
    http = RetryingHttpClient(client)
    collectors: dict[str, RepositoryCollector] = {
        "github": GitHubCollector(
            http,
            token=settings.github_token,
            base_url=settings.github_api_base,
        ),
        "huggingface": HuggingFaceCollector(
            http,
            token=settings.huggingface_token,
            base_url=settings.huggingface_api_base,
        ),
    }
    selected = list(collectors.values()) if provider == "all" else [collectors[provider]]
    return selected


def parse_args() -> argparse.Namespace:
    """Parse provider and limit arguments."""
    parser = argparse.ArgumentParser(description="Run one repository collection batch")
    parser.add_argument("--provider", choices=("github", "huggingface", "all"), default="all")
    parser.add_argument("--limit", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    """Collect one batch and print a JSON summary."""
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
