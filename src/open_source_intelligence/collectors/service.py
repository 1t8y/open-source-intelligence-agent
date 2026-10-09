"""Idempotent repository ingestion service for collector output."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.collectors.types import (
    CollectedRepository,
    CollectorError,
    RepositoryCollector,
)
from open_source_intelligence.persistence.models.intelligence import DataSource, Repository
from open_source_intelligence.security.audit import record_audit_event


@dataclass(slots=True)
class CollectionSummary:
    """Collected repository counts and provider errors."""

    provider_counts: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def ensure_data_source(session: Session, collector: RepositoryCollector) -> DataSource:
    """Return the configured data source row for a collector."""
    source = session.scalar(select(DataSource).where(DataSource.source_type == collector.provider))
    if source is None:
        source = DataSource(
            name=collector.provider.title(),
            source_type=collector.provider,
            base_url=collector.base_url,
        )
        session.add(source)
        session.flush()
    return source


def upsert_repository(
    session: Session,
    *,
    source: DataSource,
    item: CollectedRepository,
) -> Repository:
    """Insert or update one repository using its provider identity."""
    repository = session.scalar(
        select(Repository).where(
            Repository.source_id == source.id,
            Repository.provider_repo_id == item.provider_repo_id,
        )
    )
    if repository is None:
        repository = Repository(
            source_id=source.id,
            provider_repo_id=item.provider_repo_id,
            full_name=item.full_name,
            html_url=item.html_url,
            description=item.description,
            language=item.language,
            license=item.license,
            topics_json=list(item.topics),
            stars=item.stars,
            forks=item.forks,
            first_seen_at=item.observed_at,
            last_seen_at=item.observed_at,
        )
        session.add(repository)
    else:
        repository.full_name = item.full_name
        repository.html_url = item.html_url
        repository.description = item.description
        repository.language = item.language
        repository.license = item.license
        repository.topics_json = list(item.topics)
        repository.stars = item.stars
        repository.forks = item.forks
        repository.last_seen_at = item.observed_at
    return repository


def collect_from_collector(
    session: Session,
    collector: RepositoryCollector,
    *,
    limit: int,
    observed_at: datetime,
) -> int:
    """Collect, normalize, and commit one provider in a transaction."""
    source = ensure_data_source(session, collector)
    try:
        items = collector.collect(limit=limit)
        for item in items:
            upsert_repository(session, source=source, item=item)
        source.last_success_at = observed_at
        record_audit_event(
            session,
            action="collector.run",
            resource_type="data_source",
            resource_id=str(source.id),
            result="success",
            metadata_json={"provider": collector.provider, "count": len(items)},
        )
        session.commit()
        return len(items)
    except CollectorError as exc:
        session.rollback()
        source = ensure_data_source(session, collector)
        record_audit_event(
            session,
            action="collector.run",
            resource_type="data_source",
            resource_id=str(source.id),
            result="failed",
            metadata_json={"provider": collector.provider, "error": str(exc)},
        )
        session.commit()
        raise


def collect_repositories(
    session: Session,
    collectors: Sequence[RepositoryCollector],
    *,
    limit: int,
) -> CollectionSummary:
    """Collect from all requested providers and continue on source failures."""
    summary = CollectionSummary()
    for collector in collectors:
        try:
            count = collect_from_collector(
                session,
                collector,
                limit=limit,
                observed_at=datetime.now(UTC),
            )
        except CollectorError as exc:
            summary.errors[collector.provider] = str(exc)
            summary.provider_counts[collector.provider] = 0
        else:
            summary.provider_counts[collector.provider] = count
    return summary
