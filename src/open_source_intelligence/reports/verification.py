"""Verify a report artifact before it is allowed to publish.

The Verifier inspects the **persisted report**, not the Agent draft. Between composition and
publication a report can be edited or have its sources pruned, so the four `A-004` checks
(structure, citations, numbers, sensitive content) are re-run against exactly what would go
out. Every run of the current version leaves a `report_verifications` row, which is the
queryable evidence behind `INV-5`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.tools.contracts import REPORT_SECTION_KEYS, CitationRef
from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.core.logging import redact_text
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.reporting import (
    REPORT_SOURCE_TYPES,
    Report,
    ReportSource,
    ReportVerification,
)
from open_source_intelligence.security.audit import record_audit_event

from .citations import resolve_citation
from .composition import SECTION_TITLES
from .errors import ReportGenerationError

#: Mirrors `CitationRef.source_type`; a stored row is a plain string, so it must be narrowed.
SourceType = Literal["metric", "readme", "prior_report"]

VERDICT_PASSED = "passed"
VERDICT_FAILED = "failed"

#: Structure reasons.
REASON_STRUCTURE_MISSING_SECTIONS = "STRUCTURE_MISSING_SECTIONS"
#: Citation reasons.
REASON_CITATION_MISSING = "CITATION_MISSING"
REASON_CITATION_UNRESOLVED = "CITATION_UNRESOLVED"
REASON_LOW_CITATION_COVERAGE = "LOW_CITATION_COVERAGE"
#: Number reasons.
REASON_NUMBER_WITHOUT_METRIC = "NUMBER_WITHOUT_METRIC"
#: Sensitivity reasons.
REASON_SENSITIVE_CONTENT = "SENSITIVE_CONTENT"
REASON_SENSITIVITY_VISIBILITY_MISMATCH = "SENSITIVITY_VISIBILITY_MISMATCH"

PLACEHOLDER = "[REDACTED]"
MAX_DETAIL_SAMPLES = 5


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """Result of one Verifier pass over a report version."""

    report_id: int
    version: int
    verdict: str
    structure_ok: bool
    citations_ok: bool
    numbers_ok: bool
    sensitivity_ok: bool
    citation_coverage: float
    reason_codes: tuple[str, ...]
    missing_headings: tuple[str, ...]
    unresolved_refs: tuple[str, ...]
    sensitive_markers: int

    @property
    def passed(self) -> bool:
        """Return whether the report may proceed to a publish decision."""
        return self.verdict == VERDICT_PASSED

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload."""
        return {
            "report_id": self.report_id,
            "version": self.version,
            "verdict": self.verdict,
            "structure_ok": self.structure_ok,
            "citations_ok": self.citations_ok,
            "numbers_ok": self.numbers_ok,
            "sensitivity_ok": self.sensitivity_ok,
            "citation_coverage": self.citation_coverage,
            "reason_codes": list(self.reason_codes),
            "missing_headings": list(self.missing_headings),
            "unresolved_refs": list(self.unresolved_refs),
            "sensitive_markers": self.sensitive_markers,
        }


def split_section_bodies(markdown: str) -> dict[str, str]:
    """Map every level-2 heading in the rendered report to its body text."""
    bodies: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            if current is not None:
                bodies[current] = "\n".join(buffer)
            current = line[3:].strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        bodies[current] = "\n".join(buffer)
    return bodies


def count_sensitive_markers(content: str) -> int:
    """Count credential-shaped spans the shared redaction rules would remove.

    Reusing `redact_text` keeps one definition of "sensitive" for logs and reports: if the
    redactor rewrites the text, the report carries something that must not be published
    unattended.
    """
    redacted = redact_text(content)
    if redacted == content:
        return 0
    return redacted.count(PLACEHOLDER) - content.count(PLACEHOLDER)


def _check_structure(report: Report, bodies: dict[str, str]) -> tuple[bool, list[str]]:
    """Every template section must survive into the rendered artifact."""
    missing = [
        SECTION_TITLES[key] for key in REPORT_SECTION_KEYS if SECTION_TITLES[key] not in bodies
    ]
    title_ok = report.content_markdown.splitlines()[:1] == [f"# {report.title}"]
    if not title_ok:
        missing.insert(0, report.title)
    return not missing, missing


def _check_citations(
    session: Session,
    report: Report,
) -> tuple[bool, float, list[str], list[str]]:
    """Return citation health plus the per-section coverage ratio."""
    rows = session.scalars(select(ReportSource).where(ReportSource.report_id == report.id)).all()
    covered = {row.section_key for row in rows}
    missing_sections = [key for key in REPORT_SECTION_KEYS if key not in covered]
    unresolved: list[str] = []
    for row in rows:
        if not row.source_ref:
            unresolved.append(f"{row.section_key}:<empty>")
            continue
        if row.source_type not in REPORT_SOURCE_TYPES:
            unresolved.append(f"{row.section_key}:<unknown-source-type:{row.source_type}>")
            continue
        try:
            resolve_citation(
                session,
                CitationRef(
                    source_type=cast("SourceType", row.source_type),
                    source_ref=row.source_ref,
                    source_url=row.source_url,
                ),
            )
        except (ReportGenerationError, ValueError):
            unresolved.append(row.source_ref)
    coverage = (len(REPORT_SECTION_KEYS) - len(missing_sections)) / len(REPORT_SECTION_KEYS)
    return not missing_sections and not unresolved, coverage, missing_sections, unresolved


def _check_numbers(bodies: dict[str, str], metric_sections: set[str]) -> tuple[bool, list[str]]:
    """A section that states a number must cite at least one metric snapshot."""
    offenders: list[str] = []
    for key in REPORT_SECTION_KEYS:
        title = SECTION_TITLES[key]
        body = bodies.get(title, "")
        if any(character.isdigit() for character in body) and key not in metric_sections:
            offenders.append(key)
    return not offenders, offenders


def _check_sensitivity(report: Report, content: str) -> tuple[bool, int, bool]:
    """Detect credential-shaped content and an inconsistent sensitivity/visibility pair."""
    markers = count_sensitive_markers(content)
    mismatch = report.sensitivity == "CONFIDENTIAL" and report.visibility == "INTERNAL"
    return markers == 0 and not mismatch, markers, mismatch


def verify_report(
    session: Session,
    *,
    report_id: int,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> VerificationOutcome:
    """Run the four `A-004` checks and persist the verdict for the current version.

    Re-running is idempotent for a version: the existing row is refreshed instead of being
    duplicated, so the table always holds one verdict per report version.
    """
    resolved_settings = settings or get_settings()
    moment = now or utc_now()
    report = session.get(Report, report_id)
    if report is None:
        raise ReportGenerationError(f"report {report_id} does not exist")

    bodies = split_section_bodies(report.content_markdown)
    metric_sections = set(
        session.scalars(
            select(ReportSource.section_key).where(
                ReportSource.report_id == report.id,
                ReportSource.source_type == "metric",
            )
        ).all()
    )

    structure_ok, missing_headings = _check_structure(report, bodies)
    citations_ok, coverage, _missing_sections, unresolved = _check_citations(session, report)
    numbers_ok, number_offenders = _check_numbers(bodies, metric_sections)
    sensitivity_ok, markers, mismatch = _check_sensitivity(report, report.content_markdown)

    reasons: list[str] = []
    if not structure_ok:
        reasons.append(REASON_STRUCTURE_MISSING_SECTIONS)
    if not citations_ok:
        reasons.append(REASON_CITATION_UNRESOLVED if unresolved else REASON_CITATION_MISSING)
    if not numbers_ok:
        reasons.append(REASON_NUMBER_WITHOUT_METRIC)
    if markers:
        reasons.append(REASON_SENSITIVE_CONTENT)
    if mismatch:
        reasons.append(REASON_SENSITIVITY_VISIBILITY_MISMATCH)
    if coverage < resolved_settings.publish_min_citation_coverage:
        reasons.append(REASON_LOW_CITATION_COVERAGE)

    verdict = VERDICT_PASSED if not reasons else VERDICT_FAILED
    details: dict[str, Any] = {
        "missing_headings": missing_headings,
        "unresolved_refs": unresolved[:MAX_DETAIL_SAMPLES],
        "sections_without_metric": number_offenders,
        "citation_coverage": coverage,
        "sensitive_markers": markers,
        "sensitivity_visibility_mismatch": mismatch,
        "min_citation_coverage": resolved_settings.publish_min_citation_coverage,
    }

    row = session.scalar(
        select(ReportVerification).where(
            ReportVerification.report_id == report.id,
            ReportVerification.version == report.current_version,
        )
    )
    if row is None:
        row = ReportVerification(
            report_id=report.id,
            version=report.current_version,
            verdict=verdict,
        )
        session.add(row)
    row.verdict = verdict
    row.structure_ok = structure_ok
    row.citations_ok = citations_ok
    row.numbers_ok = numbers_ok
    row.sensitivity_ok = sensitivity_ok
    row.reason_codes_json = reasons
    row.details_json = details
    row.created_at = moment

    record_audit_event(
        session,
        action="report.verify",
        resource_type="report",
        resource_id=str(report.id),
        result="success" if verdict == VERDICT_PASSED else "failed",
        metadata_json={
            "version": report.current_version,
            "verdict": verdict,
            "reason_codes": reasons,
        },
    )
    session.commit()
    return VerificationOutcome(
        report_id=report.id,
        version=report.current_version,
        verdict=verdict,
        structure_ok=structure_ok,
        citations_ok=citations_ok,
        numbers_ok=numbers_ok,
        sensitivity_ok=sensitivity_ok,
        citation_coverage=coverage,
        reason_codes=tuple(reasons),
        missing_headings=tuple(missing_headings),
        unresolved_refs=tuple(unresolved),
        sensitive_markers=markers,
    )
