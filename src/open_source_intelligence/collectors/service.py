"""把采集器的输出幂等入库的服务。"""

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
"""单份缓存 README 的字符数上限。

读取这份缓存的工具本来就会截到 12000 字符，所以这个上限保护的不是模型，而是表：README 是
陌生人写的文档，它的尾部往往是好几年前的发布日志；存下几兆字节却永远只读前面一段，等于
存了没人读的数据。上限刻意高于工具自己的最大值 —— 这样调高工具的限制时，不会悄悄开始读取
一份被截断的文档。
"""


@dataclass(slots=True)
class CollectionSummary:
    """采集到的仓库数量与各供应商的错误。"""

    provider_counts: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def ensure_data_source(session: Session, collector: RepositoryCollector) -> DataSource:
    """返回某个采集器对应的、已配置的数据源行。"""
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
    """按供应商身份插入或更新一个仓库。"""
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

    # 同一个仓库可能已经以它的*名字*留档，却带着另一个供应商 id。GitHub
    # 下发的是数字 id，但在那个 id 可取之前写下的行 —— 演示数据播种器写
    # 的是 `provider_repo_id = full_name`，早期入库也是这么做的 —— 是以
    # `owner/name` 为键的。此时若只按供应商 id 匹配，就会给同一个仓库插
    # 进第二行：两份 star 数、两条快照序列，以及一个把同一个项目排两次的
    # 主题排名。唯一键拦不住它，因为两个供应商 id 确实不同。所以要收编那
    # 行已经带着该名字的记录，并修正它的供应商 id。
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
            # `first_seen_at` 保持原值：仓库当初就是在那时被首次看到的，
            # 不是现在。
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
    """对已归一化的仓库做 upsert 并 flush，但不提交。

    刻意不提交。分页回填在同一个事务里提交一页的行，以及指向它们之后的
    游标，这样一次崩溃不会留下一个已经前进、却没有它所声称的那些行的游
    标，也不会留下已入库却没有任何记录的行。正是这一点让崩溃窗口变得无
    害，而不只是不太可能。
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
    """缓存一个仓库的长文本，并返回存储的正文是否发生了变化。

    这一行只经由本函数写入，好让「已缓存」只有一个定义：由
    `clean_untrusted_text` 清洗、截到 `max_chars`、再对清洗后的文本取
    摘要。读侧（`get_repository_content`）在取出时会再清洗一遍，这是安
    全的，因为清洗是幂等的 —— 也正是它要求两侧用的是同一个辅助函数，而
    不是两个相似的函数。

    每次成功抓取都会推进 `fetched_at`，包括文本没有变化的那次，因为它
    记录的是*这份内容最后一次被确认的时间*，而内容阶段正是用它来决定今
    天是否还需要再问某个仓库一次。`content` 与 `content_hash` 只在文本
    确实变化时才动，于是一份稳定的 README 不会被每天重写一遍。

    返回：
        存储的正文发生变化（新增行，或文本不同）时为 `True`；存储的正文
        本来就是这个文本时为 `False`。

    注意：
        后写者胜。这里没有 compare-and-swap，因为每日的内容阶段是生产环
        境唯一的写入方，并且一次只处理一个仓库；两个并发写入方会在
        `(repository_id, content_type)` 唯一键上撞车并明确失败，而不会
        把两份文档各写一半交叠在一起。
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
    """在一个事务里采集、归一化并提交一个供应商。"""
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
    """向所有请求的供应商采集，某个源失败时继续。"""
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
