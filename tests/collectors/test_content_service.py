"""Tests for the single write path into `repository_contents`.

The cache has two readers - the `get_repository_content` tool and `reports/citations.py` - and one
writer. These tests pin the properties those readers depend on: the stored body is already
cleaned, its digest describes exactly that body, `fetched_at` tracks confirmation rather than
change (the contents stage's idempotence window is built on it), and a source URL that a report
citation could not carry is refused at the door.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from open_source_intelligence.collectors.service import (
    MAX_CACHED_CONTENT_CHARS,
    upsert_repository_content,
)
from open_source_intelligence.collectors.types import FetchedContent
from open_source_intelligence.core.text import clean_untrusted_text, content_digest
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.base import Base
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    RepositoryContent,
)

NOW = datetime(2026, 10, 16, 5, 0, tzinfo=UTC)
SOURCE_URL = "https://github.com/example/repo/blob/main/README.md"


@pytest.fixture()
def session() -> Session:
    """Provide a shared in-memory database with one repository in it."""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    with factory() as active:
        source = DataSource(
            name="GitHub",
            source_type="github",
            base_url="https://api.github.com",
            enabled=True,
            rate_limit_per_minute=30,
        )
        active.add(source)
        active.flush()
        active.add(
            Repository(
                source_id=source.id,
                provider_repo_id="1",
                full_name="example/repo",
                html_url="https://github.com/example/repo",
                description="test",
                language="Python",
                license="MIT",
                topics_json=["agent"],
                stars=10,
                forks=1,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )
        active.commit()
        yield active


def repository(session: Session) -> Repository:
    """Return the one seeded repository."""
    loaded = session.scalar(select(Repository))
    assert loaded is not None
    return loaded


def fetched(text: str, *, at: datetime = NOW, url: str = SOURCE_URL) -> FetchedContent:
    """Build one fetched README record."""
    return FetchedContent(
        provider="github",
        full_name="example/repo",
        content_type="readme",
        source_url=url,
        content=text,
        fetched_at=at,
    )


def stored(session: Session) -> RepositoryContent:
    """Return the one cached content row."""
    record = session.scalar(select(RepositoryContent))
    assert record is not None
    return record


def test_the_first_write_stores_cleaned_text_and_its_digest(session: Session) -> None:
    """What lands in the table is the cleaned document, and the digest describes that."""
    raw = "<script>alert(1)</script># Title\n\n\n\nBody\x00 text"

    assert upsert_repository_content(session, repository=repository(session), fetched=fetched(raw))
    record = stored(session)

    assert "<script>" not in record.content
    assert "\x00" not in record.content
    assert record.content_hash == content_digest(record.content)
    assert record.source_url == SOURCE_URL
    assert record.content_type == "readme"
    assert record.fetched_at is not None


def test_the_stored_body_is_already_what_a_second_clean_would_produce(session: Session) -> None:
    """Reading cleans again; cleaning twice must not change the text.

    If it did, the digest stored beside the body would describe a string no reader ever sees,
    and "has this README changed" would answer itself with a different value depending on who
    asked.
    """
    raw = "  # Title   \n\n\n<p>Body</p>\x07  "

    upsert_repository_content(session, repository=repository(session), fetched=fetched(raw))
    record = stored(session)

    assert clean_untrusted_text(record.content) == record.content


def test_an_unchanged_readme_is_not_rewritten_but_is_marked_as_confirmed(session: Session) -> None:
    """A stable README costs one row update for `fetched_at` and nothing else."""
    upsert_repository_content(session, repository=repository(session), fetched=fetched("# Same"))
    first = stored(session)
    first_hash = first.content_hash
    first_body = first.content
    later = NOW + timedelta(hours=20)

    changed = upsert_repository_content(
        session, repository=repository(session), fetched=fetched("# Same", at=later)
    )
    second = stored(session)

    assert changed is False
    assert second.content_hash == first_hash
    assert second.content == first_body
    assert second.fetched_at is not None and second.fetched_at > NOW


def test_changed_text_replaces_the_body_and_the_digest(session: Session) -> None:
    """A rewritten README is stored as the new document, not appended to the old one."""
    upsert_repository_content(session, repository=repository(session), fetched=fetched("# First"))
    before = stored(session).content_hash

    changed = upsert_repository_content(
        session,
        repository=repository(session),
        fetched=fetched("# Second", at=NOW + timedelta(days=1)),
    )
    after = stored(session)

    assert changed is True
    assert after.content_hash != before
    assert after.content == "# Second"
    assert session.scalar(select(func.count(RepositoryContent.id))) == 1


def test_a_readme_longer_than_the_bound_is_capped_before_it_is_stored(session: Session) -> None:
    """The bound protects the table, and the digest describes the truncated body."""
    long_body = "x" * (MAX_CACHED_CONTENT_CHARS + 500)

    upsert_repository_content(session, repository=repository(session), fetched=fetched(long_body))
    record = stored(session)

    assert len(record.content) == MAX_CACHED_CONTENT_CHARS
    assert record.content_hash == content_digest(record.content)


def test_a_source_url_a_citation_could_not_carry_is_refused(session: Session) -> None:
    """`reports/citations.py` accepts only https, so the cache must not hold anything else."""
    with pytest.raises(ValueError, match="https"):
        upsert_repository_content(
            session,
            repository=repository(session),
            fetched=fetched("# Title", url="http://example.com/README.md"),
        )
    assert session.scalar(select(func.count(RepositoryContent.id))) == 0
