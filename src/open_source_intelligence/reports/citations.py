"""依据已持久化的证据解析并校验报告引用。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.tools.contracts import CitationRef
from open_source_intelligence.persistence.models.intelligence import (
    Repository,
    RepositoryContent,
    RepoSnapshot,
)
from open_source_intelligence.persistence.models.reporting import Report

from .errors import ReportGenerationError

#: 一条指标引用指向某个仓库在某一天的快照。仓库那一段要么是数据库的 `repository_id`
#: （可变的自增主键，起草阶段使用），要么是稳定的 `owner/name` 全名（生成报告记录的就是
#: 它，这样一条引用才能熬过数据库重建）。全名是持久锚点，数字 id 只是内部主键。
_METRIC_REF = re.compile(r"repo_snapshots:(?P<repo>[^:]+):(?P<date>\d{4}-\d{2}-\d{2})")
_README_REF = re.compile(r"repository_contents:(?P<repo>[^:]+):readme")
_PRIOR_REPORT_REF = re.compile(r"reports:(?P<report_id>\d+)")


def _metric_ref(full_name: str, snapshot_date: date) -> str:
    """返回某个仓库快照的稳定指标引用字符串。"""
    return f"repo_snapshots:{full_name}:{snapshot_date.isoformat()}"


def _readme_ref(full_name: str) -> str:
    """返回某个仓库的稳定 README 引用字符串。"""
    return f"repository_contents:{full_name}:readme"


@dataclass(frozen=True, slots=True)
class ResolvedCitation:
    """一条源行存在且可追溯的引用。"""

    source_type: str
    source_ref: str
    repository_id: int | None
    source_url: str | None
    metric_snapshot_id: int | None


def _validated_url(source_url: str | None) -> str | None:
    """只接受显式的 HTTPS 链接作为面向报告的 URL。"""
    if source_url is None:
        return None
    if not source_url.startswith("https://"):
        raise ReportGenerationError(f"citation URL must use https: {source_url}")
    return source_url


def _metric_source(
    session: Session,
    *,
    repo_token: str,
    snapshot_date: date,
) -> tuple[Repository, RepoSnapshot] | None:
    """返回指标引用所指的仓库与快照；找不到时返回 `None`。

    全名必须与日期一起解析，不能单独解析。`repositories` 在
    `(source_id, provider_repo_id)` 上唯一，所以同一个 `owner/name` 可以在每个数据源下
    各存在一条；若先解析全名、再指望第一条记录恰好持有该快照，引用就会指向排序靠前的
    那个数据源。
    """
    if repo_token.isdigit():
        repository = session.get(Repository, int(repo_token))
        if repository is None:
            return None
        snapshot = session.scalar(
            select(RepoSnapshot).where(
                RepoSnapshot.repository_id == repository.id,
                RepoSnapshot.snapshot_date == snapshot_date,
            )
        )
        return None if snapshot is None else (repository, snapshot)
    row = session.execute(
        select(RepoSnapshot, Repository)
        .join(Repository, Repository.id == RepoSnapshot.repository_id)
        .where(
            Repository.full_name == repo_token,
            RepoSnapshot.snapshot_date == snapshot_date,
        )
        .order_by(Repository.id.asc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return row[1], row[0]


def _readme_source(
    session: Session,
    *,
    repo_token: str,
) -> tuple[Repository, RepositoryContent] | None:
    """返回 README 引用所指的仓库与缓存 README；找不到时返回 `None`。"""
    if repo_token.isdigit():
        repository = session.get(Repository, int(repo_token))
        if repository is None:
            return None
        content = session.scalar(
            select(RepositoryContent).where(
                RepositoryContent.repository_id == repository.id,
                RepositoryContent.content_type == "readme",
            )
        )
        return None if content is None else (repository, content)
    row = session.execute(
        select(RepositoryContent, Repository)
        .join(Repository, Repository.id == RepositoryContent.repository_id)
        .where(
            Repository.full_name == repo_token,
            RepositoryContent.content_type == "readme",
        )
        .order_by(Repository.id.asc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return row[1], row[0]


def resolve_citation(session: Session, citation: CitationRef) -> ResolvedCitation:
    """把一条引用解析成数据库中已存在的源。"""
    source_url = _validated_url(citation.source_url)
    if citation.source_type == "metric":
        match = _METRIC_REF.fullmatch(citation.source_ref)
        if match is None:
            raise ReportGenerationError(f"invalid metric citation: {citation.source_ref}")
        snapshot_date = date.fromisoformat(match.group("date"))
        metric_source = _metric_source(
            session,
            repo_token=match.group("repo"),
            snapshot_date=snapshot_date,
        )
        if metric_source is None:
            raise ReportGenerationError(f"metric source not found: {citation.source_ref}")
        repository, snapshot = metric_source
        return ResolvedCitation(
            source_type=citation.source_type,
            source_ref=_metric_ref(repository.full_name, snapshot_date),
            repository_id=repository.id,
            source_url=source_url or repository.html_url,
            metric_snapshot_id=snapshot.id,
        )

    if citation.source_type == "readme":
        match = _README_REF.fullmatch(citation.source_ref)
        if match is None:
            raise ReportGenerationError(f"invalid readme citation: {citation.source_ref}")
        readme_source = _readme_source(session, repo_token=match.group("repo"))
        if readme_source is None:
            raise ReportGenerationError(f"readme source not found: {citation.source_ref}")
        repository, content = readme_source
        return ResolvedCitation(
            source_type=citation.source_type,
            source_ref=_readme_ref(repository.full_name),
            repository_id=repository.id,
            source_url=source_url or content.source_url,
            metric_snapshot_id=None,
        )

    if citation.source_type == "prior_report":
        match = _PRIOR_REPORT_REF.fullmatch(citation.source_ref)
        if match is None:
            raise ReportGenerationError(f"invalid prior-report citation: {citation.source_ref}")
        report_id = int(match.group("report_id"))
        report = session.get(Report, report_id)
        if report is None or report.status != "published":
            raise ReportGenerationError(f"published prior report not found: {citation.source_ref}")
        return ResolvedCitation(
            source_type=citation.source_type,
            source_ref=citation.source_ref,
            repository_id=None,
            source_url=source_url,
            metric_snapshot_id=None,
        )

    raise ReportGenerationError(f"unsupported citation type: {citation.source_type}")
