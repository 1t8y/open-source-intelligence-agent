"""Resolve and validate report citations against persisted evidence."""

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

#: A metric citation points at one repository's snapshot on one date. The repository part is
#: either a database `repository_id` (a mutable auto-increment key, used while drafting) or the
#: stable `owner/name` full name (what a generated report records, so a citation survives a
#: database rebuild). The full name is the durable anchor; the numeric id is the internal key.
_METRIC_REF = re.compile(r"repo_snapshots:(?P<repo>[^:]+):(?P<date>\d{4}-\d{2}-\d{2})")
_README_REF = re.compile(r"repository_contents:(?P<repo>[^:]+):readme")
_PRIOR_REPORT_REF = re.compile(r"reports:(?P<report_id>\d+)")


def _metric_ref(full_name: str, snapshot_date: date) -> str:
    """Return the stable metric citation string for one repository snapshot."""
    return f"repo_snapshots:{full_name}:{snapshot_date.isoformat()}"


def _readme_ref(full_name: str) -> str:
    """Return the stable readme citation string for one repository."""
    return f"repository_contents:{full_name}:readme"


@dataclass(frozen=True, slots=True)
class ResolvedCitation:
    """A citation whose source row exists and is traceable."""

    source_type: str
    source_ref: str
    repository_id: int | None
    source_url: str | None
    metric_snapshot_id: int | None


def _validated_url(source_url: str | None) -> str | None:
    """Accept only explicit HTTPS links as report-facing URLs."""
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
    """Return the repository and snapshot a metric reference names, or None.

    A full name is resolved together with the date rather than on its own. `repositories` is
    unique on `(source_id, provider_repo_id)`, so the same `owner/name` can exist once per data
    source; resolving the name first and hoping the first row owns the snapshot would make a
    citation point at whichever source happened to sort first.
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
    """Return the repository and cached README a readme reference names, or None."""
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
    """Resolve one citation reference to an existing database source."""
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
