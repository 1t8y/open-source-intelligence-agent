"""Idempotent repository ingestion service for collector output."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from open_source_intelligence.collectors.types import (
    CollectedRepository,
    CollectorError,
    FetchedContent,
    RepositoryCollector,
)
from open_source_intelligence.core.text import clean_untrusted_text, content_digest, truncate_text
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    RepositoryContent,
)
from open_source_intelligence.security.audit import record_audit_event

MAX_CACHED_CONTENT_CHARS: Final[int] = 20000
"""Upper bound on one cached README, in characters.

The tool that reads this cache truncates to at most 12000 characters anyway, so this bound is
not the one that protects the model — it protects the table. A README is a document a stranger
wrote, and the far tail of one is release notes from years ago; storing megabytes of it to then
always truncate it is storage without a reader. The bound sits above the tool's own maximum so
that raising the tool's limit does not silently start reading a truncated document.
"""


@dataclass(slots=True)
class CollectionSummary:
    """Collected repository counts and provider errors."""

    provider_counts: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def ensure_data_source(session: Session, collector: RepositoryCollector) -> DataSource:
    """Return the configured data source row for a collector."""
    source = session.scalar(select(DataSource).where(DataSource.source_type == collector.provider))
    if source is None:
        if session.get_bind().dialect.name == "mysql":
            statement = mysql_insert(DataSource).values(
                name=collector.display_name,
                source_type=collector.provider,
                base_url=collector.base_url,
                enabled=True,
                rate_limit_per_minute=30,
            )
            statement = statement.on_duplicate_key_update(
                name=statement.inserted.name,
                base_url=statement.inserted.base_url,
            )
            session.execute(statement)
            source = session.scalar(
                select(DataSource).where(DataSource.source_type == collector.provider)
            )
        else:
            source = DataSource(
                name=collector.display_name,
                source_type=collector.provider,
                base_url=collector.base_url,
            )
            session.add(source)
            session.flush()
    elif source.name != collector.display_name or source.base_url != collector.base_url:
        source.name = collector.display_name
        source.base_url = collector.base_url
    if source is None:
        raise RuntimeError(f"failed to persist data source: {collector.provider}")
    return source


def upsert_repository(
    session: Session,
    *,
    source: DataSource,
    item: CollectedRepository,
) -> Repository:
    """Insert or update one repository using its provider identity."""
    values = {
        "source_id": source.id,
        "provider_repo_id": item.provider_repo_id,
        "full_name": item.full_name,
        "html_url": item.html_url,
        "description": item.description,
        "language": item.language,
        "license": item.license,
        "topics_json": list(item.topics),
        "stars": item.stars,
        "forks": item.forks,
        "open_issues": item.open_issues,
        "first_seen_at": item.observed_at,
        "last_seen_at": item.observed_at,
    }

    # The same repository can already be on file under its *name* while carrying a different
    # provider id. GitHub hands out a numeric id, but a row written before that id was available
    # — the demo seeder writes `provider_repo_id = full_name`, and so did early ingestion — keys
    # on `owner/name`. Matching only on the provider id would then insert a second row for one
    # repository: two star counts, two snapshot series, and a topic ranking the same project
    # twice. The unique key cannot prevent it, because the two provider ids genuinely differ.
    # So adopt the row that already carries the name and correct its provider id.
    known = session.scalar(
        select(Repository).where(
            Repository.source_id == source.id,
            Repository.provider_repo_id == item.provider_repo_id,
        )
    )
    if known is None:
        adopted = session.scalar(
            select(Repository).where(
                Repository.source_id == source.id,
                Repository.full_name == item.full_name,
            )
        )
        if adopted is not None:
            # `first_seen_at` stays as it was: the repository was first seen then, not now.
            for column, value in values.items():
                if column in {"source_id", "first_seen_at"}:
                    continue
                setattr(adopted, column, value)
            return adopted

    if session.get_bind().dialect.name == "mysql":
        statement = mysql_insert(Repository).values(**values)
        statement = statement.on_duplicate_key_update(
            full_name=statement.inserted.full_name,
            html_url=statement.inserted.html_url,
            description=statement.inserted.description,
            language=statement.inserted.language,
            license=statement.inserted.license,
            topics_json=statement.inserted.topics_json,
            stars=statement.inserted.stars,
            forks=statement.inserted.forks,
            open_issues=statement.inserted.open_issues,
            last_seen_at=statement.inserted.last_seen_at,
        )
        session.execute(statement)
        repository = session.scalar(
            select(Repository).where(
                Repository.source_id == source.id,
                Repository.provider_repo_id == item.provider_repo_id,
            )
        )
        if repository is None:
            raise RuntimeError("failed to persist repository")
        return repository

    repository = session.scalar(
        select(Repository).where(
            Repository.source_id == source.id,
            Repository.provider_repo_id == item.provider_repo_id,
        )
    )
    if repository is None:
        repository = Repository(**values)
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
        repository.open_issues = item.open_issues
        repository.last_seen_at = item.observed_at
    return repository


def ingest_items(
    session: Session,
    *,
    source: DataSource,
    items: Sequence[CollectedRepository],
) -> int:
    """Upsert already-normalized repositories and flush, without committing.

    Deliberately committless. A paged backfill commits one page's rows and the cursor that
    points past them in a single transaction, so a crash cannot leave a cursor that moved
    without the rows it claims, or rows ingested with no record of them. That is what makes
    the crash window harmless rather than merely unlikely.
    """
    if not items:
        return 0
    for item in items:
        upsert_repository(session, source=source, item=item)
    session.flush()
    return len(items)


def upsert_repository_content(
    session: Session,
    *,
    repository: Repository,
    fetched: FetchedContent,
    max_chars: int = MAX_CACHED_CONTENT_CHARS,
) -> bool:
    """Cache one repository's long-form text; return whether the stored body changed.

    The row is written through this one function so that "cached" has a single definition:
    cleaned by `clean_untrusted_text`, capped at `max_chars`, and digested over the cleaned
    text. The reader (`get_repository_content`) cleans again on the way out, which is safe
    because cleaning is idempotent - and it is the reason this has to be the same helper on
    both sides rather than two similar ones.

    `fetched_at` moves on every successful fetch, including one whose text did not change,
    because it records *when this content was last confirmed*, and that is what the contents
    stage uses to decide whether a repository has to be asked about again today. `content`
    and `content_hash` move only when the text actually changed, so a stable README is not
    rewritten once a day.

    Returns:
        `True` when the stored body changed (a new row, or different text), `False` when the
        stored body already was this text.

    Note:
        Last write wins. There is no compare-and-swap because the daily contents stage is the
        only production writer and it runs one repository at a time; two concurrent writers
        would collide on the `(repository_id, content_type)` unique key and fail loudly rather
        than interleave halves of two documents.
    """
    if not fetched.source_url.startswith("https://"):
        raise ValueError(f"cached content source_url must use https: {fetched.source_url}")
    cleaned, _ = truncate_text(clean_untrusted_text(fetched.content), max_chars)
    digest = content_digest(cleaned)
    record = session.scalar(
        select(RepositoryContent).where(
            RepositoryContent.repository_id == repository.id,
            RepositoryContent.content_type == fetched.content_type,
        )
    )
    if record is None:
        session.add(
            RepositoryContent(
                repository_id=repository.id,
                content_type=fetched.content_type,
                source_url=fetched.source_url,
                content=cleaned,
                content_hash=digest,
                fetched_at=fetched.fetched_at,
            )
        )
        session.flush()
        return True
    changed = record.content_hash != digest
    if changed:
        record.content = cleaned
        record.content_hash = digest
    record.source_url = fetched.source_url
    record.fetched_at = fetched.fetched_at
    session.flush()
    return changed


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
        count = ingest_items(session, source=source, items=items)
        source.last_success_at = observed_at
        record_audit_event(
            session,
            action="collector.run",
            resource_type="data_source",
            resource_id=str(source.id),
            result="success",
            metadata_json={"provider": collector.provider, "count": count},
        )
        session.commit()
        return count
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
