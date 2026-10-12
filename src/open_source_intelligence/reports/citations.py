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

_METRIC_REF = re.compile(r"repo_snapshots:(?P<repository_id>\d+):(?P<date>\d{4}-\d{2}-\d{2})")
_README_REF = re.compile(r"repository_contents:(?P<repository_id>\d+):readme")
_PRIOR_REPORT_REF = re.compile(r"reports:(?P<report_id>\d+)")


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


def resolve_citation(session: Session, citation: CitationRef) -> ResolvedCitation:
    """Resolve one citation reference to an existing database source."""
    source_url = _validated_url(citation.source_url)
    if citation.source_type == "metric":
        match = _METRIC_REF.fullmatch(citation.source_ref)
        if match is None:
            raise ReportGenerationError(f"invalid metric citation: {citation.source_ref}")
        repository_id = int(match.group("repository_id"))
        snapshot_date = date.fromisoformat(match.group("date"))
        snapshot = session.scalar(
            select(RepoSnapshot).where(
                RepoSnapshot.repository_id == repository_id,
                RepoSnapshot.snapshot_date == snapshot_date,
            )
        )
        repository = session.get(Repository, repository_id)
        if snapshot is None or repository is None:
            raise ReportGenerationError(f"metric source not found: {citation.source_ref}")
        return ResolvedCitation(
            source_type=citation.source_type,
            source_ref=citation.source_ref,
            repository_id=repository_id,
            source_url=source_url or repository.html_url,
            metric_snapshot_id=snapshot.id,
        )

    if citation.source_type == "readme":
        match = _README_REF.fullmatch(citation.source_ref)
        if match is None:
            raise ReportGenerationError(f"invalid readme citation: {citation.source_ref}")
        repository_id = int(match.group("repository_id"))
        content = session.scalar(
            select(RepositoryContent).where(
                RepositoryContent.repository_id == repository_id,
                RepositoryContent.content_type == "readme",
            )
        )
        if content is None:
            raise ReportGenerationError(f"readme source not found: {citation.source_ref}")
        return ResolvedCitation(
            source_type=citation.source_type,
            source_ref=citation.source_ref,
            repository_id=repository_id,
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
