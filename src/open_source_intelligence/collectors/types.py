"""Normalized collector data structures and errors."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class CollectorError(RuntimeError):
    """Base error for external collection failures."""


class SourceUnavailableError(CollectorError):
    """Raised when a source cannot return valid data after retries."""


@dataclass(frozen=True, slots=True)
class CollectedRepository:
    """Provider-independent repository snapshot used by the ingestion service."""

    provider: str
    provider_repo_id: str
    full_name: str
    html_url: str
    description: str | None
    language: str | None
    license: str | None
    topics: tuple[str, ...]
    stars: int
    forks: int
    observed_at: datetime


class RepositoryCollector(Protocol):
    """Contract implemented by each repository provider adapter."""

    provider: str
    base_url: str

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        """Collect up to the requested number of repositories."""
        ...
