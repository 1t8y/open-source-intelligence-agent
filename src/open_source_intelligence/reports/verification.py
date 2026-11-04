"""在允许发布之前验证报告产物。

验证器检查的是**已持久化的报告**，不是 Agent 的草稿。从编排到发布之间，报告可能被编辑，
引用也可能被裁掉，所以四项 `A-004` 检查（结构、引用、数字、敏感内容）都针对真正要发
出去的那份内容重跑。针对当前版本的每一轮验证都会留下一行 `report_verifications`，它是
`INV-5` 背后可查询的证据。
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
from .errors import ReportGenerationError, ReportNotFoundError

#: 与 `CitationRef.source_type` 对齐；库里存的是普通字符串，所以必须收窄类型。
SourceType = Literal["metric", "readme", "prior_report"]

VERDICT_PASSED = "passed"
VERDICT_FAILED = "failed"

#: 结构类理由。
REASON_STRUCTURE_MISSING_SECTIONS = "STRUCTURE_MISSING_SECTIONS"
#: 引用类理由。
REASON_CITATION_MISSING = "CITATION_MISSING"
REASON_CITATION_UNRESOLVED = "CITATION_UNRESOLVED"
REASON_LOW_CITATION_COVERAGE = "LOW_CITATION_COVERAGE"
#: 数字类理由。
REASON_NUMBER_WITHOUT_METRIC = "NUMBER_WITHOUT_METRIC"
#: 敏感性类理由。
REASON_SENSITIVE_CONTENT = "SENSITIVE_CONTENT"
REASON_SENSITIVITY_VISIBILITY_MISMATCH = "SENSITIVITY_VISIBILITY_MISMATCH"

PLACEHOLDER = "[REDACTED]"
MAX_DETAIL_SAMPLES = 5


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """验证器对某个报告版本做一轮检查的结果。"""

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
        """返回该报告是否可以进入发布决策。"""
        return self.verdict == VERDICT_PASSED

    def to_dict(self) -> dict[str, Any]:
        """返回可安全转 JSON 的载荷。"""
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
    """把渲染后报告里每个二级标题映射到它的正文。"""
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
    """数出共享脱敏规则会移除的、形似凭据的片段。

    复用 `redact_text`，让日志和报告共用同一份「敏感」定义：只要脱敏器改写了文本，就
    说明报告里带着不该无人值守发布的东西。
    """
    redacted = redact_text(content)
    if redacted == content:
        return 0
    return redacted.count(PLACEHOLDER) - content.count(PLACEHOLDER)


def _check_structure(report: Report, bodies: dict[str, str]) -> tuple[bool, list[str]]:
    """检查模板的每个章节是否都保留到了渲染后的产物里。"""
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
    """返回当前版本的引用健康状况，以及逐章节的覆盖率。"""
    rows = session.scalars(
        select(ReportSource).where(
            ReportSource.report_id == report.id,
            ReportSource.version == report.current_version,
        )
    ).all()
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
    """检查写了数字的章节是否至少引用了一个指标快照。"""
    offenders: list[str] = []
    for key in REPORT_SECTION_KEYS:
        title = SECTION_TITLES[key]
        body = bodies.get(title, "")
        if any(character.isdigit() for character in body) and key not in metric_sections:
            offenders.append(key)
    return not offenders, offenders


def _check_sensitivity(report: Report, content: str) -> tuple[bool, int, bool]:
    """检测形似凭据的内容，以及自相矛盾的敏感级别 / 可见性组合。"""
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
    """跑完四项 `A-004` 检查，并为当前版本持久化结论。

    对同一个版本重跑是幂等的：刷新已有的行而不是重复插入，所以表里每个报告版本永远
    只有一条结论。
    """
    resolved_settings = settings or get_settings()
    moment = now or utc_now()
    report = session.get(Report, report_id)
    if report is None:
        raise ReportNotFoundError(f"report {report_id} does not exist")

    bodies = split_section_bodies(report.content_markdown)
    metric_sections = set(
        session.scalars(
            select(ReportSource.section_key).where(
                ReportSource.report_id == report.id,
                ReportSource.version == report.current_version,
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
