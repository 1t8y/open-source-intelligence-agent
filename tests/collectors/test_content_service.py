"""守护写入 `repository_contents` 的唯一写路径。

这份缓存有两个读者 —— `get_repository_content` 工具与 `reports/citations.py` ——
却只有一个写者。下面这些用例钉住读者所依赖的性质：入库的正文已经清洗过、
摘要描述的正是这段正文、`fetched_at` 记录的是「确认」而非「变化」（`contents`
阶段的幂等窗口就建在它上面），以及报告引用无法承载的来源 URL 会在入口被拒。
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
    """提供一个已含一个仓库的共享内存数据库。"""
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
    """取出唯一那个已播种的仓库。"""
    loaded = session.scalar(select(Repository))
    assert loaded is not None
    return loaded


def fetched(text: str, *, at: datetime = NOW, url: str = SOURCE_URL) -> FetchedContent:
    """构造一条拉取到的 README 记录。"""
    return FetchedContent(
        provider="github",
        full_name="example/repo",
        content_type="readme",
        source_url=url,
        content=text,
        fetched_at=at,
    )


def stored(session: Session) -> RepositoryContent:
    """取出唯一那条缓存正文行。"""
    record = session.scalar(select(RepositoryContent))
    assert record is not None
    return record


def test_the_first_write_stores_cleaned_text_and_its_digest(session: Session) -> None:
    """落表的应当是清洗后的文档，摘要描述的也必须是这段文本。"""
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
    """读者会再清洗一遍，因此清洗两次不能改变文本。

    若会改变，与正文一起存下的摘要描述的将是一个任何读者都见不到的字符串，而
    「这份 README 变了吗」的答案会随提问者不同而不同。
    """
    raw = "  # Title   \n\n\n<p>Body</p>\x07  "

    upsert_repository_content(session, repository=repository(session), fetched=fetched(raw))
    record = stored(session)

    assert clean_untrusted_text(record.content) == record.content


def test_an_unchanged_readme_is_not_rewritten_but_is_marked_as_confirmed(session: Session) -> None:
    """README 未变时，唯一该发生的写入是更新 `fetched_at`，正文与摘要不动。"""
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
    """重写过的 README 以新文档覆盖存储，而不是追加到旧文档之后。"""
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
    """上限保护的是表体积，摘要则必须描述截断之后的那段正文。"""
    long_body = "x" * (MAX_CACHED_CONTENT_CHARS + 500)

    upsert_repository_content(session, repository=repository(session), fetched=fetched(long_body))
    record = stored(session)

    assert len(record.content) == MAX_CACHED_CONTENT_CHARS
    assert record.content_hash == content_digest(record.content)


def test_a_source_url_a_citation_could_not_carry_is_refused(session: Session) -> None:
    """`reports/citations.py` 只接受 https，缓存里就不能存别的协议。"""
    with pytest.raises(ValueError, match="https"):
        upsert_repository_content(
            session,
            repository=repository(session),
            fetched=fetched("# Title", url="http://example.com/README.md"),
        )
    assert session.scalar(select(func.count(RepositoryContent.id))) == 0
